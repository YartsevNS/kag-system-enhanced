"""Восстановление таблиц из страниц, где текстовый слой не помог (сканы, картинки).

Зачем модуль: у нас два разных инструмента дают разное, и их надо свести в один результат —
нормальную таблицу (строки × колонки) для нашего табличного слоя.

  * **Сетка от Occular** (`TableRecognizer`) — быстрая и детерминированная, но отдаёт только сетку
    (диапазоны координат строк и колонок), а текст в ячейки нужно раскладывать самим по боксам OCR.
    Работает на таблицах с линиями (счета-фактуры, квитанции, накладные): проверено 27.09.2026 —
    на квитанции находит 2 таблицы за 1,3 с (сетки 8×9 и 21×13).
  * **Модель зрения** (Qwen2-VL-2B в Ollama) — отдаёт готовую markdown-таблицу и вытаскивает числа
    там, где наш OCR их теряет. Это единственный путь для таблиц БЕЗ линий (цветные заливки ячеек,
    скриншоты из Excel): детектор по линиям на них находит 0 таблиц, а обычный OCR теряет цифры
    (замер 27.09.2026 на файле владельца 790×796: числа 4, 28, 2, 43 восстановила именно модель).

Оба пути приводятся к одному виду — `RecoveredTable`, и дальше пишутся в наш существующий табличный
слой (document_tables / table_rows) с указанием источника и страницы (провенанс).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

BBox = Tuple[float, float, float, float]      # x0, y0, x1, y1

SOURCE_OCCULAR_GRID = "occular-grid"
SOURCE_VLM = "vlm"


@dataclass
class OcrLine:
    """Распознанная строка текста с координатами — то, что отдаёт наш OCR."""
    text: str
    bbox: BBox


@dataclass
class RecoveredTable:
    """Восстановленная таблица в общем виде: строки × колонки плюс провенанс."""
    rows: List[List[str]]
    source: str
    source_model: str = ""
    bbox: Optional[BBox] = None
    confidence: Optional[float] = None
    quality: Optional[float] = None            # честная оценка пригодности таблицы (0..1)
    page: int = 0
    seconds: float = 0.0                       # сколько заняло получение таблицы
    notes: List[str] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def is_usable(self) -> bool:
        """Таблица годится, если есть хотя бы две строки и две колонки: одна строка — это не таблица."""
        return self.n_rows >= 2 and self.n_cols >= 2


def _center(box: BBox) -> Tuple[float, float]:
    x0, y0, x1, y1 = box
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def _index_of(ranges: Sequence[Tuple[float, float]], value: float) -> Optional[int]:
    for i, (lo, hi) in enumerate(ranges):
        if lo <= value < hi:
            return i
    if ranges and value >= ranges[-1][1]:
        return len(ranges) - 1
    if ranges and value < ranges[0][0]:
        return 0
    return None


def cells_from_grid(row_ranges: Sequence[Tuple[float, float]],
                    col_ranges: Sequence[Tuple[float, float]],
                    lines: Sequence[OcrLine]) -> List[List[str]]:
    """Разложить текст OCR по сетке таблицы.

    Строку кладём в ячейку по её центру: если центр попал в строку i и колонку j — текст идёт туда.
    Несколько строк в одной ячейке соединяем пробелом (в ячейках таблиц это обычное дело: текст
    переносится по строкам).

    Ограничение (осознанное): строка, растянутая на несколько колонок (объединённая ячейка), целиком
    попадёт в ту колонку, где её центр. Для объединённых ячеек нужна модель структуры таблиц, которой
    у нас нет (её веса опциональны и не скачаны), поэтому такие случаи помечаются в notes — см.
    `cells_from_grid_annotated`.
    """
    rows, _ = cells_from_grid_annotated(row_ranges, col_ranges, lines)
    return rows


def cells_from_grid_annotated(row_ranges: Sequence[Tuple[float, float]],
                              col_ranges: Sequence[Tuple[float, float]],
                              lines: Sequence[OcrLine]) -> Tuple[List[List[str]], List[str]]:
    """То же, что `cells_from_grid`, но возвращает и замечания (для провенанса и отладки)."""
    if not row_ranges or not col_ranges:
        return [], ["пустая сетка: нет строк или колонок"]

    grid: List[List[str]] = [["" for _ in col_ranges] for _ in row_ranges]
    notes: List[str] = []
    for line in lines:
        text = (line.text or "").strip()
        if not text:
            continue
        cx, cy = _center(line.bbox)
        r = _index_of(row_ranges, cy)
        c = _index_of(col_ranges, cx)
        if r is None or c is None:
            notes.append(f"строка вне сетки: {text[:40]!r}")
            continue
        # Растянутая строка: её края выходят за границы колонки более чем на её ширину.
        lo, hi = col_ranges[c]
        width = hi - lo
        x0, _, x1, _ = line.bbox
        if width > 0 and (x1 - x0) > 1.5 * width:
            notes.append(f"похоже на объединённую ячейку: {text[:40]!r}")
        if grid[r][c]:
            grid[r][c] = f"{grid[r][c]} {text}"
        else:
            grid[r][c] = text
    return grid, notes


def table_from_markdown(markdown: str, source_model: str = "", page: int = 0) -> Optional[RecoveredTable]:
    """Разобрать ответ модели: markdown-таблица → строки и ячейки.

    Берём самый большой непрерывный блок строк, начинающихся с «|» (модель иногда добавляет
    пояснения до и после), строку-разделитель «|---|---|» выбрасываем.
    """
    blocks: List[List[str]] = []
    current: List[str] = []
    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if line.startswith("|") and line.count("|") >= 2:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    if not blocks:
        return None

    best = max(blocks, key=len)
    rows: List[List[str]] = []
    for line in best:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(set(c) <= {"-", ":", " "} and c for c in cells):
            continue                      # разделитель шапки
        rows.append(cells)
    if not rows:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return RecoveredTable(rows=rows, source=SOURCE_VLM, source_model=source_model, page=page)

def to_markdown(rows: List[List[str]]) -> str:
    """Собрать markdown-таблицу из ячеек (её читает и чат, и табличный слой)."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]
    head = "| " + " | ".join(str(c).replace("|", "\\|") for c in rows[0]) + " |"
    sep = "|" + "|".join(["---"] * width) + "|"
    body = ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows[1:]]
    return "\n".join([head, sep] + body)


def to_html(rows: List[List[str]]) -> str:
    """Собрать HTML-таблицу: первая строка — шапка (так же устроены таблицы из парсера)."""
    if not rows:
        return ""
    import html as _html

    head = "".join(f"<th>{_html.escape(str(c))}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{_html.escape(str(c))}</td>" for c in r) + "</tr>"
                   for r in rows[1:])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


class _CellsHTMLParser:
    """Собирает строки и ячейки из HTML-таблицы (стандартная библиотека, без зависимостей).

    Нужен, если модель зрения отвечает HTML: у него есть colspan/rowspan, которых markdown не умеет
    выражать вовсе. Разметку модели нельзя считать идеальной, поэтому парсер терпимый: битые теги
    просто не попадут в результат, а не сломают обработку.
    """

    def __init__(self) -> None:
        from html.parser import HTMLParser

        self.rows: List[List[str]] = []
        self._row: List[str] = []
        self._cell: List[str] = []
        self._in_cell = False
        self._span: List[int] = []
        outer = self

        class _Impl(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag in ("td", "th"):
                    outer._in_cell = True
                    outer._cell = []
                    span = 1
                    for k, v in attrs or []:
                        if k == "colspan":
                            try:
                                span = max(1, int(str(v).strip()))
                            except (TypeError, ValueError):
                                span = 1
                    outer._span = [span]
                elif tag == "tr":
                    outer._row = []

            def handle_endtag(self, tag):
                if tag in ("td", "th") and outer._in_cell:
                    outer._in_cell = False
                    text = " ".join("".join(outer._cell).split())
                    span = outer._span[0] if outer._span else 1
                    outer._row.extend([text] + [""] * (span - 1))
                elif tag == "tr" and outer._row:
                    outer.rows.append(outer._row)
                    outer._row = []

            def handle_data(self, data):
                if outer._in_cell:
                    outer._cell.append(data)

        self._impl = _Impl()

    def feed(self, text: str) -> None:
        try:
            self._impl.feed(text or "")
        except Exception:  # noqa: BLE001 — битая разметка не должна ломать обработку
            pass
        if self._row:
            self.rows.append(self._row)
            self._row = []


def rows_from_html(text: str) -> List[List[str]]:
    """Разобрать HTML-таблицу в строки и ячейки (colspan разворачиваем в пустые ячейки)."""
    parser = _CellsHTMLParser()
    parser.feed(text or "")
    return [r for r in parser.rows if any(c for c in r)]


def rows_from_json(text: str) -> List[List[str]]:
    """Разобрать ответ модели вида JSON-массива строк (терпимо к обёртке и тексту вокруг)."""
    import json

    cleaned = (text or "").strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end < 0 or end <= start:
        return []
    try:
        data = json.loads(cleaned[start:end + 1])
    except Exception:  # noqa: BLE001
        return []
    rows: List[List[str]] = []
    if isinstance(data, list):
        for row in data:
            if isinstance(row, list):
                rows.append([str(c) for c in row])
            elif isinstance(row, dict):
                rows.append([str(v) for v in row.values()])
    return rows
