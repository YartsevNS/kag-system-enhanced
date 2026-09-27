"""Сетка таблицы по линиям бланка + раскладка текста по ячейкам (без библиотечной модели структуры).

Зачем свой модуль, а не TableRecognizer из Occular: на реальном скане накладной библиотечный разбор
структуры вернул 23 строки нулевой высоты из 30 (проверено 27.09.2026), а текст в ячейки он не кладёт
вовсе. При этом линии бланка видны отлично: свой разбор даёт 29 горизонтальных и 21 вертикальную линию
за 0,13 с. Поэтому сетку строим сами, а текст берём тем же распознаванием Occular — качество проверено:
низкоуровневый распознаватель с языковой моделью на вырезке строки даёт уверенность 0,78–0,97.

Ключевые решения:
  * **увеличение** перед обработкой (×3): у низкоразрежённых сканов текст в ячейке ~8 px высотой, и на таком
    размере распознавание даёт мусор;
  * **объединённые ячейки** получаются сами: в каждой строке учитываются только те вертикальные линии,
    которые в ней реально нарисованы — где линии нет, ячейка шире;
  * **разрезка строк**: распознанная строка может накрывать несколько колонок («796 шт 796 шт 796 шт») —
    такую строку режем по линиям колонок и распознаём каждый кусок отдельно.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.indexing.table_recovery import RecoveredTable

SOURCE_GRID = "occular-grid"

INSET = 2               # отступ от линий, чтобы сами линии не попадали в распознавание
MIN_CELL_PX = 6
SCALE = 3              # увеличение перед обработкой
MIN_ROWS = 3           # меньше — это не таблица
MIN_COLS = 2
LINE_COVER_H = 0.5     # горизонтальная линия должна занимать половину ширины таблицы
LINE_COVER_V = 0.10    # вертикальная — десятую часть высоты


@dataclass
class Grid:
    """Сетка таблицы: полосы строк и, для каждой полосы, вертикальные линии, которые в ней есть."""
    rows: List[Tuple[float, float]]
    row_lines: List[List[float]]
    lines_h: List[float]
    lines_v: List[float]
    bbox: Tuple[float, float, float, float]
    notes: List[str] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def max_cols(self) -> int:
        return max((len(l) - 1 for l in self.row_lines), default=0)


def _group(values: Sequence[int], gap: int = 4) -> List[int]:
    """Слить близкие координаты в одну линию (толщина линии на скане — несколько пикселей)."""
    out: List[List[int]] = []
    for v in values:
        if out and v - out[-1][-1] <= gap:
            out[-1].append(v)
        else:
            out.append([v])
    return [int(round(sum(g) / len(g))) for g in out]


def detect_grid(image_bgr: Any, scale: int = SCALE) -> Optional[Any]:
    """Найти сетку таблицы по линиям. Возвращает (grid, масштабированное изображение) или (None, image)."""
    import cv2

    if scale != 1:
        image = cv2.resize(image_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    else:
        image = image_bgr
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    bw = cv2.adaptiveThreshold(255 - gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 30), 1)))
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, h // 40))))
    hproj, vproj = (horiz > 0).sum(axis=1), (vert > 0).sum(axis=0)
    lines_v = _group([j for j, v in enumerate(vproj) if v > h * LINE_COVER_V])
    if len(lines_v) < MIN_COLS + 1:
        return None, image
    y_min = min(int(np.argmax(vert[:, x] > 0)) for x in lines_v)
    y_max = max(h - 1 - int(np.argmax(vert[::-1, x] > 0)) for x in lines_v)
    x_min, x_max = min(lines_v), max(lines_v)
    lines_h = _group([y_min + i for i, v in enumerate(hproj[y_min:y_max + 1])
                      if v > (x_max - x_min) * LINE_COVER_H])
    if len(lines_h) < MIN_ROWS + 1:
        return None, image

    rows: List[Tuple[float, float]] = []
    row_lines: List[List[float]] = []
    for i in range(len(lines_h) - 1):
        y0, y1 = lines_h[i] + INSET, lines_h[i + 1] - INSET
        if y1 - y0 < MIN_CELL_PX:
            continue
        present = [x for x in lines_v
                   if float((vert[max(0, y0):y1, max(0, x - 1):x + 2] > 0).mean()) > 0.5]
        present = sorted(set([x_min] + present + [x_max]))
        if len(present) < MIN_COLS + 1:
            continue
        rows.append((float(y0), float(y1)))
        row_lines.append([float(x) for x in present])
    if len(rows) < MIN_ROWS:
        return None, image
    return Grid(rows=rows, row_lines=row_lines, lines_h=[float(v) for v in lines_h],
                lines_v=[float(v) for v in lines_v],
                bbox=(float(x_min), float(y_min), float(x_max), float(y_max))), image


def _col_of(row_lines: Sequence[float], x: float) -> Optional[int]:
    for i in range(len(row_lines) - 1):
        if row_lines[i] <= x < row_lines[i + 1]:
            return i
    if row_lines and x >= row_lines[-1]:
        return len(row_lines) - 2
    return None


def _row_of(grid: Grid, y: float) -> Optional[int]:
    for i, (y0, y1) in enumerate(grid.rows):
        if y0 <= y < y1:
            return i
    return None


def fill_cells(image: Any, grid: Grid, lines: Sequence[Dict[str, Any]],
               recognize_cells: Optional[Callable[[Any, List[np.ndarray]], List[Tuple[str, float]]]] = None,
               page: int = 0) -> RecoveredTable:
    """Разложить распознанные строки по ячейкам сетки.

    Строка внутри одной колонки идёт в ячейку как есть. Строка, накрывающая несколько колонок, режется по
    линиям колонок, и каждый кусок распознаётся отдельно (`recognize_cells`). Если распознаватель не передан,
    такая строка попадает в колонку своего центра — это хуже, но не теряет данные.
    """
    cells: Dict[Tuple[int, int], List[str]] = {}
    to_split: List[Tuple[int, int, np.ndarray]] = []
    notes: List[str] = []

    for line in lines:
        text = str(line.get("text") or "").strip()
        quad = line.get("quad")
        if quad is None:
            box = line.get("bbox")
            if box is None:
                continue
            quad = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
        if not text:
            continue
        arr = np.asarray(quad, dtype=np.float32).reshape(-1, 2)
        x0, y0 = float(arr[:, 0].min()), float(arr[:, 1].min())
        x1, y1 = float(arr[:, 0].max()), float(arr[:, 1].max())
        ri = _row_of(grid, (y0 + y1) / 2)
        if ri is None:
            continue
        row_lines = grid.row_lines[ri]
        ci_first = _col_of(row_lines, x0 + 1)
        ci_last = _col_of(row_lines, x1 - 1)
        if ci_first is None:
            continue
        if ci_last is not None and ci_last > ci_first:
            if recognize_cells is None:
                cells.setdefault((ri, ci_first), []).append(text)
                continue
            for ci in range(ci_first, ci_last + 1):
                left = max(x0, row_lines[ci])
                right = min(x1, row_lines[ci + 1])
                if right - left < MIN_CELL_PX:
                    continue
                to_split.append((ri, ci, np.array([[left, y0], [right, y0], [right, y1], [left, y1]],
                                                  dtype=np.float32)))
        else:
            cells.setdefault((ri, ci_first), []).append(text)

    if to_split and recognize_cells is not None:
        quads = [q for _, _, q in to_split]
        try:
            got = recognize_cells(image, quads)
        except Exception as e:  # noqa: BLE001 — распознавание кусков не должно терять уже собранное
            notes.append(f"куски строк распознать не удалось: {type(e).__name__}: {str(e)[:80]}")
            got = []
        for (ri, ci, _), item in zip(to_split, got):
            text = str(item[0] if isinstance(item, (list, tuple)) else item).strip()
            if text:
                cells.setdefault((ri, ci), []).append(text)
        notes.append(f"разрезано строк на куски: {len(to_split)}")

    width = grid.max_cols
    rows_out: List[List[str]] = []
    for ri in range(grid.n_rows):
        row = []
        for ci in range(width):
            cell = cells.get((ri, ci))
            row.append(" ".join(cell) if cell else "")
        rows_out.append(row)

    table = RecoveredTable(rows=rows_out, source=SOURCE_GRID, source_model="occular-grid",
                           bbox=grid.bbox, page=page, notes=notes + grid.notes)
    table.quality = table_quality(table.rows)
    return table


def table_quality(rows: Sequence[Sequence[str]]) -> float:
    """Честная оценка таблицы вместо прежней заглушки 0,6.

    Считаем: долю заполненных ячеек и долю осмысленных (не односимвольных) среди заполненных.
    Пустая таблица и таблица из одиночных символов получают низкую оценку — по ней принимается решение
    вызывать ли модель зрения.
    """
    if not rows:
        return 0.0
    width = max(len(r) for r in rows)
    total = len(rows) * max(1, width)
    filled = [str(c).strip() for r in rows for c in r if str(c).strip()]
    if not filled:
        return 0.0
    meaningful = sum(1 for c in filled if len(c) > 2 or any(ch.isdigit() for ch in c))
    fill_ratio = len(filled) / max(1, total)
    meaning_ratio = meaningful / len(filled)
    # Вес: заполненность важнее (пустая таблица бесполезна), осмысленность — второй множитель.
    return round(min(1.0, 0.5 * fill_ratio + 0.5 * meaning_ratio * min(1.0, fill_ratio * 3)), 3)
