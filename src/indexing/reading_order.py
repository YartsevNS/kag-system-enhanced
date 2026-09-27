"""Порядок чтения страницы: собрать распознанные строки в читаемый текст по геометрии.

Зачем: сейчас текст скана уходит во фрагменты в порядке распознавания строк — получается «цио- прослеживаемости
нальное) и 16 le и 126 12 6 22,10 Без лицо 2016 796…». Так было и до таблиц; на этом фоне любая ошибка выглядит
как полный мусор. В промышленных конвейерах это решается явно: у PP-StructureV3 порядок чтения — отдельный
артефакт (block_order), у MinerU он даже нарисован на отладочной странице. Модель для этого не нужна — порядок
восстанавливается по геометрии.

Алгоритм (три шага, каждый проверяем тестом):

  1. **Колонки.** Ищем вертикальные «коридоры» — полосы по x, которые не перекрывает ни одна строка на большей
     части высоты. Это разделители колонок в многоколоночной вёрстке; в одноколоночном документе коридоров нет.
  2. **Полосы строк.** Внутри колонки строки группируются в горизонтальные полосы по вертикальному перекрытию:
     строка таблицы с несколькими ячейками на одной высоте становится одной полосой, и ячейки читаются слева
     направо — это и есть правильный порядок для таблиц.
  3. **Абзацы и переносы.** Полосы, стоящие друг под другом без большого разрыва, склеиваются в абзац; слово,
     разорванное переносом в конце строки, соединяется обратно.

На выходе — текст и список блоков с координатами: блоки нужны просмотрщику, чтобы подсвечивать область на
странице, а текст — фрагментам поиска.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Коридор: полоса без строк шириной не меньше этой доли ширины страницы и не у самых краёв
GUTTER_MIN_SHARE = 0.030
GUTTER_COVER_MAX = 0.05          # покрытие коридора строками по высоте
GUTTER_MAX_SHARE = 0.25          # шире этого — уже не коридор, а поля страницы
COLUMN_MIN_LINES = 2             # колонка должна содержать хотя бы столько строк
BAND_OVERLAP = 0.5               # доля высоты строки для попадания в ту же полосу
PARA_GAP = 1.7                   # разрыв между полосами (в долях высоты) — граница абзаца
TABLE_CELLS = 4                  # столько ячеек в полосе считаем табличной строкой
TABLE_DIGIT_SHARE = 0.6          # и столько из них с цифрами


@dataclass
class Block:
    """Блок текста: сам текст и рамка на странице (для наложения в просмотрщике)."""
    text: str
    bbox: Tuple[float, float, float, float]
    lines: int = 1
    is_table_row: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "bbox": list(self.bbox), "lines": self.lines,
                "is_table_row": self.is_table_row}


@dataclass
class PageOrder:
    """Результат: текст страницы и блоки."""
    text: str = ""
    blocks: List[Block] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def n_blocks(self) -> int:
        return len(self.blocks)


def _box(line: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    quad = line.get("quad")
    if quad is not None:
        try:
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
            return min(xs), min(ys), max(xs), max(ys)
        except Exception:  # noqa: BLE001
            return None
    box = line.get("bbox")
    if box is not None and len(box) == 4:
        return float(box[0]), float(box[1]), float(box[2]), float(box[3])
    return None


def find_gutters(boxes: Sequence[Tuple[float, float, float, float]], width: float,
                 height: float) -> List[Tuple[float, float]]:
    """Вертикальные коридоры: полосы по x, которые почти не перекрыты строками.

    Коридор — признак разделителя колонок. Края страницы коридорами не считаются.
    """
    if width <= 0 or height <= 0 or not boxes:
        return []
    step = max(2.0, width / 400.0)
    n = int(width / step) + 1
    cover = [0.0] * n
    for x0, y0, x1, y1 in boxes:
        i0, i1 = max(0, int(x0 / step)), min(n - 1, int(x1 / step))
        h = max(1.0, y1 - y0)
        for i in range(i0, i1 + 1):
            cover[i] += h
    total_h = float(height)
    free = [i for i, c in enumerate(cover) if c / total_h <= GUTTER_COVER_MAX]
    min_width = GUTTER_MIN_SHARE * width
    max_width = GUTTER_MAX_SHARE * width
    gutters: List[Tuple[float, float]] = []
    run_start: Optional[int] = None
    for i in range(n):
        if i in set(free):
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                a, b = run_start * step, i * step
                if min_width <= b - a <= max_width and a > 0.02 * width:
                    gutters.append((a, b))
                run_start = None
    # Хвост до правого края — правое поле страницы, а не разделитель колонок: не берём его.
    return gutters



def _split_columns(items: List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]],
                   gutters: Sequence[Tuple[float, float]]
                   ) -> List[List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]]]:
    """Разложить строки по колонкам, разрезанным коридорами.

    Строка, перекрывающая коридор (заголовок на всю ширину), попадает в свою «полноширинную» группу.
    """
    if not gutters:
        return [items]
    bounds: List[Tuple[float, float]] = []
    prev = None
    for g in gutters:
        if prev is None:
            bounds.append((-1e9, g[0]))
        else:
            bounds.append((prev[1], g[0]))
        prev = g
    bounds.append((prev[1], 1e9))

    groups: Dict[int, List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]]] = {}
    full_width: List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]] = []
    for item in items:
        x0, _, x1, _ = item[1]
        assigned = None
        for gi, (a, b) in enumerate(bounds):
            if x0 >= a - 1 and x1 <= b + 1:
                assigned = gi
                break
        if assigned is None:
            full_width.append(item)          # перекрывает коридор — широкая строка
        else:
            groups.setdefault(assigned, []).append(item)

    out: List[List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]]] = []
    for gi in sorted(groups):
        if len(groups[gi]) >= COLUMN_MIN_LINES:
            out.append(groups[gi])
        else:
            full_width.extend(groups[gi])
    if full_width and not out:
        out.append(full_width)
    elif full_width:
        out.append(full_width)               # широкие строки — отдельной группой (читаем их первыми)
    return out


def _bands(items: List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]]
           ) -> List[List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]]]:
    """Сгруппировать строки в горизонтальные полосы по вертикальному перекрытию."""
    ordered = sorted(items, key=lambda t: (t[1][1], t[1][0]))
    bands: List[List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]]] = []
    for item in ordered:
        y0, y1 = item[1][1], item[1][3]
        h = max(1.0, y1 - y0)
        placed = False
        if bands:
            last = bands[-1]
            ly0 = min(i[1][1] for i in last)
            ly1 = max(i[1][3] for i in last)
            overlap = min(y1, ly1) - max(y0, ly0)
            if overlap >= BAND_OVERLAP * h:
                last.append(item)
                placed = True
        if not placed:
            bands.append([item])
    for band in bands:
        band.sort(key=lambda t: t[1][0])
    return bands


def _join_hyphen(left: str, right: str) -> str:
    """Склеить перенос: «цио-» + «прослеживаемости» → «циопроизводимости»."""
    if left.endswith("-") and right and right[0].islower():
        return left[:-1] + right
    return left + " " + right


def order_page(lines: Sequence[Dict[str, Any]]) -> PageOrder:
    """Собрать текст страницы в порядке чтения. На вход — строки распознавания (текст + рамка)."""
    items: List[Tuple[Dict[str, Any], Tuple[float, float, float, float]]] = []
    for line in lines:
        text = str(line.get("text") or "").strip()
        box = _box(line)
        if not text or box is None:
            continue
        items.append(({**line, "text": text}, box))
    if not items:
        return PageOrder(notes=["строк для сборки текста нет"])

    width = max(b[2] for _, b in items)
    height = max(b[3] for _, b in items)
    gutters = find_gutters([b for _, b in items], width, height)
    notes: List[str] = []
    if gutters:
        notes.append(f"найдено коридоров (разделителей колонок): {len(gutters)}")

    columns = _split_columns(items, gutters)
    if len(columns) > 1:
        notes.append(f"колонок: {len(columns)}")

    blocks: List[Block] = []
    for column in columns:
        bands = _bands(column)
        if not bands:
            continue
        heights = [max(1.0, max(i[1][3] for i in b) - min(i[1][1] for i in b)) for b in bands]
        median_h = sorted(heights)[len(heights) // 2]
        current: List[str] = []
        cur_lines = 0
        cur_box: Optional[List[float]] = None
        cur_table = False
        prev_y1: Optional[float] = None

        def flush() -> None:
            nonlocal current, cur_lines, cur_box, cur_table, prev_y1
            if current and cur_box:
                blocks.append(Block(text=" ".join(current).strip(),
                                    bbox=(cur_box[0], cur_box[1], cur_box[2], cur_box[3]),
                                    lines=cur_lines, is_table_row=cur_table))
            current, cur_lines, cur_box, cur_table = [], 0, None, False

        for band, band_h in zip(bands, heights):
            texts = [i[0]["text"] for i in band]
            digits = sum(1 for t in texts if any(ch.isdigit() for ch in t))
            is_table = (len(texts) >= TABLE_CELLS and digits / max(1, len(texts)) >= TABLE_DIGIT_SHARE)
            band_text = (" | ".join(texts) if is_table else " ".join(texts)).strip()
            y0 = min(i[1][1] for i in band)
            y1 = max(i[1][3] for i in band)
            x0 = min(i[1][0] for i in band)
            x1 = max(i[1][2] for i in band)
            big_gap = prev_y1 is not None and (y0 - prev_y1) > PARA_GAP * median_h
            if big_gap or cur_table != is_table:
                flush()
            if current:
                current[-1] = _join_hyphen(current[-1], band_text)
            else:
                current.append(band_text)
            cur_lines += len(band)
            cur_table = is_table
            cur_box = [x0, y0, x1, y1] if cur_box is None else [
                min(cur_box[0], x0), min(cur_box[1], y0), max(cur_box[2], x1), max(cur_box[3], y1)]
            prev_y1 = y1
        flush()

    text = "\n\n".join(b.text for b in blocks)
    return PageOrder(text=text, blocks=blocks, notes=notes)
