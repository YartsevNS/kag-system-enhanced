"""Тип таблицы (числовая / прозаическая) и склейка строк внутри ячейки.

Зачем: от типа зависит способ сборки. Числовую таблицу наша сетка разбирает построчно и это верно;
прозаическая таблица (конспект, план урока) содержит в ячейках абзацы из нескольких визуальных строк,
и построчный разбор её дробит: замер 04.10.2026 — наш grid дал 38×3, SLANet на кропе 17×3 при истинных
4×3. Поэтому мало выбрать модель: нужен признак типа и склейка строк внутри ячейки.

Признак выбран ПО ЗАМЕРУ, а не по доле чисел на странице (внешняя эвристика с порогом 0,40 на нашей
смете давала неверный ответ: 0,22 по странице и 0,37 по области таблицы, оба ниже порога). Поколочный
признак на тех же двух документах разделил их чисто:

    смета «Договор 09.11.2020» (числовая 8×3): столбец «№» — 0,86 чисел, «Стоимость» — 0,78 → 2 числовых
    конспект 13611481-3 (прозаическая 4×3): все четыре столбца — 0,00 чисел → 0 числовых

Правило: два и более столбца, где ≥ половины блоков — числа, означают числовую таблицу. Калибровка
пока на двух документах — правило рабочее и объяснимое, но при новом корпусе пороги надо перепроверить.

Модуль чистый (без ввода-вывода): координаты и тексты приходят снаружи, ответ — словарь с причиной.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Доля числовых блоков в столбце, при которой столбец считается числовым.
NUMERIC_COLUMN_SHARE = 0.5
#: Минимум блоков в столбце: по одному-двум блокам судить нельзя.
MIN_BLOCKS_IN_COLUMN = 2
#: Сколько числовых столбцов делают таблицу числовой.
NUMERIC_COLUMNS_NEEDED = 2
#: Допуск кластеризации по X — доля медианной ширины блока.
COLUMN_TOLERANCE = 0.6

_NUM_ONLY = re.compile(r"^[-+]?\d*\.?\d+$")
_DATE = re.compile(r"^\d{2}[.\-/]\d{2}[.\-/]\d{4}$")


def is_number(text: str) -> bool:
    """Число ли блок: цифры, суммы, даты, проценты. Буквы и текст — нет."""
    value = (text or "").strip().replace("\u00a0", "").replace(" ", "")
    if not value:
        return False
    if value.endswith("%"):
        value = value[:-1]
    value = value.replace(",", ".")
    return bool(_NUM_ONLY.match(value) or _DATE.match(value))


def line_box(line: Any) -> Optional[Tuple[float, float, float, float]]:
    """Рамка блока: понимает и `quad` (4 точки), и `bbox` ([x0,y0,x1,y1])."""
    if isinstance(line, dict):
        quad = line.get("quad")
        if quad is not None:
            try:
                xs = [float(p[0]) for p in quad]
                ys = [float(p[1]) for p in quad]
                return min(xs), min(ys), max(xs), max(ys)
            except Exception:  # noqa: BLE001
                return None
        bbox = line.get("bbox")
        if bbox is not None and len(bbox) >= 4:
            try:
                return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
            except Exception:  # noqa: BLE001
                return None
        return None
    try:  # (x0, y0, x1, y1)
        return (float(line[0]), float(line[1]), float(line[2]), float(line[3]))
    except Exception:  # noqa: BLE001
        return None


def line_text(line: Any) -> str:
    if isinstance(line, dict):
        return str(line.get("text") or "")
    return str(line[3] if len(line) > 3 else "")


def center(box: Sequence[float]) -> Tuple[float, float]:
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def lines_in_box(lines: Sequence[Any], box: Sequence[float]) -> List[Any]:
    """Блоки, чей центр попал в рамку (рамка — (x0, y0, x1, y1))."""
    inside: List[Any] = []
    for line in lines:
        b = line_box(line)
        if b is None:
            continue
        cx, cy = center(b)
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            inside.append(line)
    return inside


def split_columns(lines: Sequence[Any]) -> List[List[Any]]:
    """Разложить блоки по столбцам: 1-D кластеризация центров по X.

    Только для ОПРЕДЕЛЕНИЯ ТИПА таблицы, не для сборки: кривой скан может дать лишний столбец-осколок
    (на смете так и вышло — 5 столбцов вместо 4), но вердикт это не меняет.
    """
    items: List[Tuple[float, Any, float]] = []
    for line in lines:
        b = line_box(line)
        if b is None:
            continue
        items.append((center(b)[0], line, max(1.0, b[2] - b[0])))
    if not items:
        return []
    widths = sorted(w for _, _, w in items)
    tol = COLUMN_TOLERANCE * widths[len(widths) // 2]
    columns: List[List[Tuple[float, Any, float]]] = []
    for item in sorted(items, key=lambda i: i[0]):
        for column in columns:
            if abs(item[0] - sum(c[0] for c in column) / len(column)) <= tol:
                column.append(item)
                break
        else:
            columns.append([item])
    return [[line for _, line, _ in sorted(column, key=lambda c: c[0])] for column in columns]


def column_stats(lines: Sequence[Any]) -> List[Dict[str, Any]]:
    """По каждому столбцу: сколько блоков и какая доля из них числа."""
    stats: List[Dict[str, Any]] = []
    for column in split_columns(lines):
        numeric = sum(1 for line in column if is_number(line_text(line)))
        stats.append({
            "blocks": len(column),
            "numeric": numeric,
            "share": round(numeric / len(column), 3) if column else 0.0,
            "sample": line_text(column[0])[:40] if column else "",
        })
    return stats


def route(lines: Sequence[Any],
          numeric_share: float = NUMERIC_COLUMN_SHARE,
          min_blocks: int = MIN_BLOCKS_IN_COLUMN,
          need_columns: int = NUMERIC_COLUMNS_NEEDED) -> Dict[str, Any]:
    """Определить тип таблицы по её блокам.

    Возвращает `{"kind": "numeric"|"prose", "reason", "columns", "numeric_columns", "blocks"}`.
    Пустой вход — «prose» с честной причиной (судить не по чему).
    """
    stats = column_stats(lines)
    if not stats:
        return {"kind": "prose", "reason": "блоков нет — тип определить нечем",
                "columns": 0, "numeric_columns": 0, "blocks": 0}

    numeric_columns = [s for s in stats
                       if s["share"] >= numeric_share and s["blocks"] >= min_blocks]
    kind = "numeric" if len(numeric_columns) >= need_columns else "prose"
    reason = (f"столбцов {len(stats)}, числовых {len(numeric_columns)} "
              f"(нужно {need_columns} при доле ≥{numeric_share:g} и ≥{min_blocks} блоков)")
    if numeric_columns:
        reason += "; числовые: " + ", ".join(
            f"«{s['sample']}» {s['share']:.2f}" for s in numeric_columns[:3])
    return {"kind": kind, "reason": reason, "columns": len(stats),
            "numeric_columns": len(numeric_columns), "blocks": sum(s["blocks"] for s in stats)}


def merge_cell_lines(cell_box: Sequence[float], lines: Sequence[Any],
                     min_overlap: float = 0.5) -> str:
    """Собрать текст ячейки из ВСЕХ её блоков, в порядке чтения.

    Почему не «один блок с максимальным перекрытием»: в прозаических таблицах ячейка содержит абзац
    из нескольких визуальных строк, и выбор одного блока молча теряет остальной текст (на конспекте
    13611481-3 это 4-5 строк на ячейку). Берём блоки, чей центр внутри рамки ИЛИ которые перекрывают
    её больше чем наполовину своей ширины, и склеиваем сверху вниз, слева направо.

    Строки одного ряда определяются по ПЕРЕКРЫТИЮ высот, а не по округлению центра: округление
    раскидывало соседей по разным полосам из-за разницы в пару пикселей (поймано тестом).
    """
    picked: List[Tuple[Sequence[float], str]] = []
    for line in lines:
        b = line_box(line)
        if b is None:
            continue
        cx, cy = center(b)
        inside = cell_box[0] <= cx <= cell_box[2] and cell_box[1] <= cy <= cell_box[3]
        width = max(1.0, b[2] - b[0])
        overlap = max(0.0, min(cell_box[2], b[2]) - max(cell_box[0], b[0]))
        if inside or overlap / width >= min_overlap:
            text = line_text(line).strip()
            if text:
                picked.append((b, text))

    if not picked:
        return ""

    picked.sort(key=lambda item: (item[0][1], item[0][0]))     # сверху вниз, затем слева вправо
    rows: List[List[Tuple[Sequence[float], str]]] = []
    for box, text in picked:
        for row in rows:
            row_top = min(b[0][1] for b in row)  # noqa: F841 (оставляем форму для наглядности)
            row_bottom = max(b[0][3] for b in row)
            height = min(row_bottom - row_top, box[3] - box[1])
            shift = min(row_bottom, box[3]) - max(row_top, box[1])
            if height > 0 and shift >= min_overlap * height:
                row.append((box, text))
                break
        else:
            rows.append([(box, text)])

    parts: List[str] = []
    for row in rows:
        parts.extend(text for _, text in sorted(row, key=lambda item: item[0][0]))
    return " ".join(parts)


def merge_cells(cells: Sequence[Sequence[float]], lines: Sequence[Any]) -> List[str]:
    """Текст по всем ячейкам: `merge_cell_lines` для каждой рамки по порядку."""
    return [merge_cell_lines(cell, lines) for cell in cells]


def status() -> Dict[str, Any]:
    """Пороги признака — для диагностики (менять их надо осознанно, с перезамером)."""
    return {"numeric_column_share": NUMERIC_COLUMN_SHARE,
            "min_blocks_in_column": MIN_BLOCKS_IN_COLUMN,
            "numeric_columns_needed": NUMERIC_COLUMNS_NEEDED,
            "column_tolerance": COLUMN_TOLERANCE}
