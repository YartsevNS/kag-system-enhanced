"""Сетка таблицы по линиям бланка + раскладка текста по ячейкам (без библиотечной модели структуры).

Зачем свой модуль, а не TableRecognizer из Occular: на реальном скане накладной библиотечный разбор
структуры вернул 23 строки нулевой высоты из 30 (проверено 27.09.2026), а текст в ячейки он не кладёт
вовсе. При этом линии бланка видны отлично: свой разбор даёт десятки линий за десятые доли секунды.
Поэтому сетку строим сами, а текст берём тем же распознаванием Occular.

Ключевые решения (каждое — из замера или разбора ошибок):

  * **выравнивание страницы** перед поиском линий. Иначе на наклонённом скане координаты сетки и координаты
    распознанных строк расходятся (внутри Occular свой deskew, а сетка строится по исходной картинке) и
    разбор ломается целиком. Распознавание тоже вызывается с выключенным внутренним deskew, чтобы обе части
    работали в одной системе координат;
  * **адаптивное увеличение**, а не «×3 на всё»: мелкий скан (текст ~8 px) увеличиваем заметно, крупный
    (300 dpi) не трогаем. Масштаб выбирается по измеренной высоте строки сетки;
  * **объединённые ячейки** получаются сами: в каждой полосе строки учитываются только реально нарисованные
    вертикальные линии — где линии нет, ячейка шире;
  * **назначение по перекрытию**: строка относится к колонке, с которой перекрывается больше всего, и режется
    только там, где в распознанном тексте на этой границе есть пробел. Число, разрезанное границей
    («18 ^» + «06.652»), остаётся целиком в одной колонке.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.indexing.table_recovery import RecoveredTable

SOURCE_GRID = "occular-grid"

INSET = 2                 # отступ от линий, чтобы сами линии не попадали в распознавание
MIN_CELL_PX = 6
MIN_ROWS = 3              # меньше — это не таблица
MIN_COLS = 2
LINE_COVER_H = 0.5        # горизонтальная линия должна занимать половину ширины таблицы
# Насколько вертикальная линия должна «проходить» через полосу строки, чтобы считать её границей
# этой строки. 0,5 было слишком строго для сканов: линия тонкая и в полосе шапки набирала 0,33 —
# из-за этого интервал шапки выбрасывался и первая строка данных становилась «шапкой»
# (найдено 04.10.2026 на скане сметы). Окно поиска линии тоже расширено до ±2 px: на сканах
# линия гуляет на пару пикселей.
LINE_ROW_COVER = 0.25
LINE_COVER_V = 0.10       # вертикальная — десятую часть высоты
TARGET_ROW_PX = 36        # желаемая высота строки после увеличения (текст ~24 px)
MAX_SCALE = 4.0
MAX_WIDTH_PX = 6000       # предел ширины обрабатываемого изображения
SPLIT_MIN_SHARE_COL = 0.5  # строка «своя» для колонки, если закрывает её не меньше чем наполовину
MAX_SKEW_DEG = 4.0        # в этих пределах страницу выравниваем


@dataclass
class Grid:
    """Сетка таблицы: полосы строк и, для каждой полосы, вертикальные линии, которые в ней есть."""
    rows: List[Tuple[float, float]]
    row_lines: List[List[float]]
    lines_h: List[float]
    lines_v: List[float]
    bbox: Tuple[float, float, float, float]
    scale: float = 1.0
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


def _line_masks(gray: Any) -> Tuple[Any, Any, Any, Any]:
    """Маски горизонтальных и вертикальных линий + их проекции."""
    import cv2

    h, w = gray.shape
    bw = cv2.adaptiveThreshold(255 - gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 30), 1)))
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, h // 40))))
    return horiz, vert, (horiz > 0).sum(axis=1), (vert > 0).sum(axis=0)


def deskew_angle(image_bgr: Any, max_deg: float = MAX_SKEW_DEG) -> float:
    """Оценка наклона страницы по профилю линий.

    Перебираем углы в пределах ±max_deg на уменьшенной копии и берём тот, при котором горизонтальные линии
    «собираются» (разброс горизонтальной проекции максимален). Приём не требует настройки порогов детектора.
    """
    import cv2

    h, w = image_bgr.shape[:2]
    k = min(1.0, 900.0 / max(h, w)) if max(h, w) > 900 else 1.0
    small = cv2.resize(image_bgr, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1.0 else image_bgr
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    best_angle, best_score = 0.0, -1.0
    angle = -max_deg
    while angle <= max_deg + 1e-9:
        if abs(angle) < 1e-9:
            rotated = gray
        else:
            m = cv2.getRotationMatrix2D((gray.shape[1] / 2, gray.shape[0] / 2), angle, 1.0)
            rotated = cv2.warpAffine(gray, m, (gray.shape[1], gray.shape[0]),
                                     flags=cv2.INTER_NEAREST, borderValue=255)
        _, _, hproj, _ = _line_masks(rotated)
        score = float(np.var(hproj))
        if score > best_score:
            best_score, best_angle = score, angle
        angle += 0.25
    return round(best_angle, 2)


def rotate_image(image_bgr: Any, angle: float) -> Any:
    """Повернуть страницу на найденный угол (фон белый, размер сохраняется)."""
    import cv2

    if abs(angle) < 0.05:
        return image_bgr
    h, w = image_bgr.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(image_bgr, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))


def _grid_at(image: Any, notes: List[str]) -> Optional[Grid]:
    """Разбор сетки на уже подготовленном (выровненном и увеличенном) изображении."""
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    _, vert, hproj, vproj = _line_masks(gray)
    lines_v = _group([j for j, v in enumerate(vproj) if v > h * LINE_COVER_V])
    if len(lines_v) < MIN_COLS + 1:
        return None
    y_min = min(int(np.argmax(vert[:, x] > 0)) for x in lines_v)
    y_max = max(h - 1 - int(np.argmax(vert[::-1, x] > 0)) for x in lines_v)
    x_min, x_max = min(lines_v), max(lines_v)
    lines_h = _group([y_min + i for i, v in enumerate(hproj[y_min:y_max + 1])
                      if v > (x_max - x_min) * LINE_COVER_H])
    # Верхняя и нижняя границы таблицы берутся из протяжённости ВЕРТИКАЛЬНЫХ линий: горизонтальная
    # линия рамки сверху/снизу бывает прервана текстом или светлее остальных и не дотягивает до
    # порога покрытия. Без этого шапка выпадала из сетки, и первой строкой становилась первая строка
    # данных — она же уезжала в шапку (найдено 04.10.2026 на скане сметы: шапка OCR на y≈530,
    # а сетка начиналась с y=584).
    if lines_h and (lines_h[0] - y_min) > MIN_CELL_PX:
        lines_h = [int(y_min)] + list(lines_h)
    if lines_h and (y_max - lines_h[-1]) > MIN_CELL_PX:
        lines_h = list(lines_h) + [int(y_max)]
    if len(lines_h) < MIN_ROWS + 1:
        return None

    rows: List[Tuple[float, float]] = []
    row_lines: List[List[float]] = []
    for i in range(len(lines_h) - 1):
        y0, y1 = lines_h[i] + INSET, lines_h[i + 1] - INSET
        if y1 - y0 < MIN_CELL_PX:
            continue
        present = [x for x in lines_v
                   if float((vert[max(0, y0):y1, max(0, x - 2):x + 3] > 0).mean()) > LINE_ROW_COVER]
        present = sorted(set([x_min] + present + [x_max]))
        if len(present) < MIN_COLS + 1:
            continue
        rows.append((float(y0), float(y1)))
        row_lines.append([float(x) for x in present])
    if len(rows) < MIN_ROWS:
        return None
    return Grid(rows=rows, row_lines=row_lines, lines_h=[float(v) for v in lines_h],
                lines_v=[float(v) for v in lines_v],
                bbox=(float(x_min), float(y_min), float(x_max), float(y_max)), notes=notes)


def detect_grid(image_bgr: Any, scale: Optional[float] = None) -> Tuple[Optional[Grid], Any]:
    """Найти сетку таблицы. Возвращает (сетка или None, подготовленное изображение).

    Порядок: выравнивание → пробный разбор → увеличение по измеренной высоте строки → разбор сетки.
    Увеличение хранится в grid.scale, применённый наклон — в grid.notes.
    """
    import cv2

    angle = deskew_angle(image_bgr)
    image = rotate_image(image_bgr, angle)
    notes: List[str] = []
    if abs(angle) >= 0.25:
        notes.append(f"страница выровнена на {angle:.2f}°")

    if scale is None:
        probe = _grid_at(image, notes)
        if probe is None:
            return None, image
        pitch = float(np.median(np.diff(probe.lines_h))) if len(probe.lines_h) > 2 else 0.0
        scale = 1.0 if pitch <= 0 else max(1.0, min(MAX_SCALE, round(TARGET_ROW_PX / pitch, 1)))
        if scale < 1.25:
            probe.scale = 1.0
            probe.notes = notes
            return probe, image
    if scale and scale != 1:
        w = image.shape[1]
        if w * scale > MAX_WIDTH_PX:
            scale = max(1.0, MAX_WIDTH_PX / w)
            notes.append(f"увеличение ограничено шириной: ×{scale:.2f}")
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    grid = _grid_at(image, notes)
    if grid is not None:
        grid.scale = float(scale)
        notes.append(f"увеличение ×{scale:.2f}")
        grid.notes = notes
    return grid, image


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


def _overlaps(cut: Sequence[float], x0: float, x1: float) -> List[Tuple[int, float]]:
    """Перекрытие отрезка [x0, x1] с колонками сетки: список (номер колонки, площадь перекрытия)."""
    out: List[Tuple[int, float]] = []
    for i in range(len(cut) - 1):
        ov = min(x1, cut[i + 1]) - max(x0, cut[i])
        if ov > 0:
            out.append((i, float(ov)))
    return out


def fill_cells(image: Any, grid: Grid, lines: Sequence[Dict[str, Any]],
               recognize_cells: Optional[Callable[[Any, List[np.ndarray]], List[Tuple[str, float]]]] = None,
               page: int = 0, split_lines: Optional[Sequence[float]] = None) -> RecoveredTable:
    """Разложить распознанные строки по ячейкам сетки.

    Назначение — по площади перекрытия с колонкой (в какую колонку строка попала большей частью). Разрезка —
    только по тем границам, где в тексте есть пробел: иначе число, разрезанное линией («18 ^» + «06.652»),
    распадалось бы на две ячейки. Границы для разрезки берутся из полного набора вертикальных линий документа,
    а не из линий одной полосы, — в полосе с объединёнными ячейками линий меньше, и широкая ячейка смешала бы
    значения (именно так и было на накладной: «796 шт Балка MS Pro 18…»).
    """
    cut = list(split_lines) if split_lines else [x for x in grid.lines_v if grid.bbox[0] <= x <= grid.bbox[2]]
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

        overlaps = _overlaps(cut, x0, x1)
        if not overlaps:
            continue
        main_col = max(overlaps, key=lambda t: t[1])[0]
        width = max(1.0, x1 - x0)
        # Колонка считается «своей» для этой строки, если строка закрывает её не меньше чем наполовину.
        # Мера — доля от ширины САМОЙ колонки, а не от ширины строки: иначе строка, накрывающая семь
        # колонок (по 14% ширины на каждую), не резалась бы вовсе, а касание соседней колонки на пару
        # пикселей наоборот давало бы ложный кусок (это и был «сдвиг на колонку» на накладной).
        cand = [(i, o) for i, o in overlaps if o >= max(MIN_CELL_PX, SPLIT_MIN_SHARE_COL * (cut[i + 1] - cut[i]))]
        if len(cand) <= 1 or recognize_cells is None:
            cells.setdefault((ri, main_col), []).append(text)
            continue

        # Группируем колонки: там, где граница попадает ВНУТРЬ слова или числа, резать нельзя — склеиваем.
        groups: List[List[Tuple[int, float]]] = []
        for k, item in enumerate(cand):
            if k == 0:
                groups.append([item])
                continue
            bx = cut[item[0]]
            pos = int(round((bx - x0) / width * len(text)))
            left = text[max(0, pos - 1)] if pos > 0 else " "
            right = text[pos] if pos < len(text) else " "
            if left.isspace() or right.isspace():
                groups.append([item])
            else:
                groups[-1].append(item)
        if len(groups) == 1:
            cells.setdefault((ri, main_col), []).append(text)
            continue
        for group in groups:
            first = group[0][0]
            last = group[-1][0]
            left = max(x0, cut[first])
            right = min(x1, cut[first + 1]) if len(group) == 1 else min(x1, cut[last + 1])
            if right - left < MIN_CELL_PX:
                continue
            to_split.append((ri, first, np.array([[left, y0], [right, y0], [right, y1], [left, y1]],
                                                 dtype=np.float32)))

    if to_split and recognize_cells is not None:
        quads = [q for _, _, q in to_split]
        try:
            got = recognize_cells(image, quads)
        except Exception as e:  # noqa: BLE001 — распознавание кусков не должно терять уже собранное
            notes.append(f"куски строк распознать не удалось: {type(e).__name__}: {str(e)[:80]}")
            got = []
        for (ri, ci, _), item in zip(to_split, got):
            piece = str(item[0] if isinstance(item, (list, tuple)) else item).strip()
            if piece:
                cells.setdefault((ri, ci), []).append(piece)
        notes.append(f"разрезано строк на куски: {len(to_split)}")

    width_cols = max(grid.max_cols, len(cut) - 1)
    rows_out: List[List[str]] = []
    for ri in range(grid.n_rows):
        row = []
        for ci in range(width_cols):
            cell = cells.get((ri, ci))
            row.append(" ".join(cell) if cell else "")
        rows_out.append(row)

    table = RecoveredTable(rows=rows_out, source=SOURCE_GRID, source_model="occular-grid",
                           bbox=grid.bbox, page=page, notes=notes + grid.notes)
    table.quality = table_quality(table.rows)
    return table


def refine_table_cells(image: Any, grid: "Grid", table: RecoveredTable,
                       recognize_cells: Callable[[Any, List[np.ndarray]], List[Tuple[str, float]]],
                       min_height: float = MIN_CELL_PX) -> int:
    """Перечитать текст ячеек по вырезкам — чтобы к ячейкам применить свой движок и язык.

    Зачем это нужно: обычно текст ячеек приходит из распознавания СТРОК страницы (один проход OCR по
    всему изображению — быстрее всего), и тогда отдельная модель для ячеек (например, «eslav» для цифр)
    ни на что не влияет. Этот проход берёт bbox каждой ячейки прямо из сетки и распознаёт их пачкой тем
    движком, который выбран для ячеек.

    Возвращает число ячеек, текст которых заменён. По умолчанию НЕ вызывается: распознавание вырезки
    на широкой ячейке может оказаться хуже распознавания всей строки, поэтому это осознанная опция.
    """
    quads: List[np.ndarray] = []
    positions: List[Tuple[int, int]] = []
    for ri, (y0, y1) in enumerate(grid.rows):
        cut = grid.row_lines[ri]
        for ci in range(len(cut) - 1):
            x0 = cut[ci] + INSET
            x1 = cut[ci + 1] - INSET
            if x1 - x0 < MIN_CELL_PX or y1 - y0 < min_height:
                continue
            quads.append(np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32))
            positions.append((ri, ci))
    if not quads:
        return 0
    try:
        got = recognize_cells(image, quads)
    except Exception:  # noqa: BLE001 — дочитывание не должно ломать уже собранную таблицу
        return 0
    changed = 0
    for (ri, ci), item in zip(positions, got):
        text = str(item[0] if isinstance(item, (list, tuple)) else item).strip()
        if not text:
            continue
        if ri < len(table.rows) and ci < len(table.rows[ri]):
            table.rows[ri][ci] = text
            changed += 1
    return changed


def table_quality(rows: Sequence[Sequence[str]]) -> float:
    """Честная оценка таблицы вместо прежней заглушки 0,6.

    Считаем: долю заполненных ячеек и долю осмысленных (не односимвольных, но с цифрой) среди заполненных.
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
    return round(min(1.0, 0.5 * fill_ratio + 0.5 * meaning_ratio * min(1.0, fill_ratio * 3)), 3)
