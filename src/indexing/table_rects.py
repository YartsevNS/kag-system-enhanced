"""Сетка таблицы из ВЕКТОРНОЙ графики PDF: правила (линии рамок) → ячейки.

Зачем отдельно от find_tables: в PDF нет объекта «таблица». У электронных документов сетка
нарисована графикой, причём по-разному, и это надо уметь читать:
  * рамки как ТОНКИЕ ЗАЛИТЫЕ прямоугольники (0,5 пт) — так делают конвертеры Word/печатных
    форм (проверено на 13611481-3.pdf: 0,5x471,8 по вертикали и 214,9x0,5 по горизонтали);
  * рамки как ОТРЕЗКИ (`l`);
  * рамки как КОНТУР прямоугольника (stroke без заливки) — синтетика и часть издательских PDF.
А ещё в файлах полно ЛОЖНЫХ прямоугольников: белые подложки под текстом (13,4x464,4 при заливке
белым), подсветки, декоративные плашки. Их надо игнорировать, иначе сетка рассыпается на десятки
колонок — на этом я уже обжёгся (27x42 на конспекте урока).

Что делаем: собираем ПРАВИЛА (горизонтальные и вертикальные линии), из их координат строим
границы строк и колонок, а объединённые ячейки определяем по ОТСУТСТВИЮ внутренней линии
(нет линии между двумя строками у этой колонки → строки слиты).

Публичные функции:
    extract_tables_from_vectors(page) -> list[dict]   # таблицы страницы
    tables_to_payload(page, tables)  -> list[dict]    # формат как у find_tables-пути
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

TOL = 2.5              # допуск склейки координат, пт
RULE_MAX_THICK = 2.0   # фигура тоньше этого — линия рамки, а не ячейка
RULE_MIN_LEN = 8.0     # короче — мусор (точка, засечка)
MIN_COLS = 2
MIN_ROWS = 2
MAX_ROWS = 400
MAX_COLS = 40


def _cluster(values: List[float], tol: float = TOL) -> List[float]:
    """Склеить близкие координаты в одну границу (возвращает отсортированные центры)."""
    out: List[List[float]] = []
    for v in sorted(values):
        if out and abs(v - out[-1][-1]) <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(g) / len(g) for g in out]


def _rules_from_page(page) -> Tuple[List[Tuple[float, float, float]], List[Tuple[float, float, float]]]:
    """Вернуть (горизонтальные, вертикальные) правила; правило = (координата, начало, конец)."""
    import fitz

    h_rules: List[Tuple[float, float, float]] = []   # (y, x0, x1)
    v_rules: List[Tuple[float, float, float]] = []   # (x, y0, y1)
    try:
        drawings = page.get_drawings()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[table-rules] get_drawings: {e}")
        return [], []

    def add_rect_sides(r) -> None:
        """Контур прямоугольника: четыре стороны становятся правилами."""
        h_rules.append((r.y0, r.x0, r.x1))
        h_rules.append((r.y1, r.x0, r.x1))
        v_rules.append((r.x0, r.y0, r.y1))
        v_rules.append((r.x1, r.y0, r.y1))

    for d in drawings:
        stroked = d.get("color") is not None and (d.get("width") or 0) > 0
        filled_white = d.get("fill") is not None and min(d["fill"]) > 0.85
        for item in d.get("items", []):
            if item[0] == "re":
                r = fitz.Rect(item[1])
                if r.width <= RULE_MAX_THICK and r.height >= RULE_MIN_LEN:
                    v_rules.append(((r.x0 + r.x1) / 2.0, r.y0, r.y1))       # тонкая вертикаль
                elif r.height <= RULE_MAX_THICK and r.width >= RULE_MIN_LEN:
                    h_rules.append(((r.y0 + r.y1) / 2.0, r.x0, r.x1))       # тонкая горизонталь
                elif filled_white or d.get("fill") is not None:
                    continue                                                 # фон/плашка — мимо
                elif stroked:
                    add_rect_sides(r)                                        # контур ячейки
            elif item[0] == "l":
                p0, p1 = item[1], item[2]
                if abs(p1.y - p0.y) <= 1.5 and abs(p1.x - p0.x) >= RULE_MIN_LEN:
                    h_rules.append((p0.y, min(p0.x, p1.x), max(p0.x, p1.x)))
                elif abs(p1.x - p0.x) <= 1.5 and abs(p1.y - p0.y) >= RULE_MIN_LEN:
                    v_rules.append((p0.x, min(p0.y, p1.y), max(p0.y, p1.y)))
    return h_rules, v_rules


def _has_h_rule(h_rules: List[Tuple[float, float, float]], y: float, x0: float, x1: float) -> bool:
    return any(abs(ry - y) <= TOL and rx0 <= x0 + TOL and rx1 >= x1 - TOL for ry, rx0, rx1 in h_rules)


def _has_v_rule(v_rules: List[Tuple[float, float, float]], x: float, y0: float, y1: float) -> bool:
    return any(abs(rx - x) <= TOL and ry0 <= y0 + TOL and ry1 >= y1 - TOL for rx, ry0, ry1 in v_rules)


def _join_cell(spans: List[Dict[str, Any]]) -> str:
    """Текст ячейки: строки сверху вниз, внутри строки — слева направо."""
    spans = sorted(spans, key=lambda s: (round(s["cy"] / 4.0), s["cx"]))
    lines: List[str] = []
    cur: List[str] = []
    cur_y = None
    for s in spans:
        if cur_y is None or abs(s["cy"] - cur_y) > max(4.0, s["h"] * 0.6):
            if cur:
                lines.append(" ".join(cur))
            cur, cur_y = [s["text"]], s["cy"]
        else:
            cur.append(s["text"])
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(t.strip() for t in lines if t.strip())


def _texts_by_cell(page, xb: List[float], yb: List[float]) -> Dict[Tuple[int, int], str]:
    """Разложить текст страницы по клеткам: центр фрагмента → содержащая клетка."""
    buckets: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    try:
        page_dict = page.get_text("dict")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[table-rules] get_text(dict): {e}")
        return {}
    for block in page_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = (span.get("text") or "").strip()
                if not txt:
                    continue
                x0, y0, x1, y1 = span["bbox"]
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                if not (xb[0] - TOL <= cx <= xb[-1] + TOL and yb[0] - TOL <= cy <= yb[-1] + TOL):
                    continue
                # клетка = интервал между двумя соседними границами: берём последнюю
                # границу, которая не правее/ниже центра, и это и есть левая/верхняя граница
                ci = 0
                for i, b in enumerate(xb):
                    if b <= cx + TOL:
                        ci = i
                    else:
                        break
                ri = 0
                for i, b in enumerate(yb):
                    if b <= cy + TOL:
                        ri = i
                    else:
                        break
                ci = max(0, min(len(xb) - 2, ci))
                ri = max(0, min(len(yb) - 2, ri))
                buckets.setdefault((ri, ci), []).append(
                    {"text": txt, "cx": cx, "cy": cy, "h": y1 - y0})
    return {k: _join_cell(v) for k, v in buckets.items()}


def extract_tables_from_vectors(page) -> List[Dict[str, Any]]:
    """Сетка страницы из правил. Пусто — полной сетки по векторной графике не видно.

    Строгость намеренная: берём ТОЛЬКО полную сетку (колонки — длинные вертикальные правила,
    строки — горизонтальные правила, пересекающие эти колонки). Иначе на странице легко принять
    за таблицу декоративные подложки (проверено на 13611481-3.pdf: строки там нарисованы белыми
    полосами, а не линиями, и без строгого фильтра сетка рассыпалась на 5 колонок и мусор).
    Не собралась полная сетка → возвращаем пусто, и решение принимает find_tables.
    """
    h_rules, v_rules = _rules_from_page(page)
    if len(h_rules) < 2 or len(v_rules) < 2:
        return []

    # Колонки = ДЛИННЫЕ вертикальные правила (то, что реально рисует сетку)
    v_sorted = sorted(v_rules, key=lambda r: -(r[2] - r[1]))
    longest = v_sorted[0][2] - v_sorted[0][1]
    v_grid = [r for r in v_rules if (r[2] - r[1]) >= max(RULE_MIN_LEN, longest * 0.5)]
    if len(v_grid) < 2:
        return []
    x_left = min(r[0] for r in v_grid)
    x_right = max(r[0] for r in v_grid)
    table_y0 = min(r[1] for r in v_grid)
    table_y1 = max(r[2] for r in v_grid)

    # Строки = горизонтальные правила, пересекающие колонки и лежащие в их высоте
    def overlaps(rx0: float, rx1: float) -> bool:
        inter = min(rx1, x_right) - max(rx0, x_left)
        return inter >= max(RULE_MIN_LEN, (rx1 - rx0) * 0.5)

    h_grid = [r for r in h_rules
              if overlaps(r[1], r[2]) and table_y0 - TOL <= r[0] <= table_y1 + TOL]
    if len(h_grid) < 2:
        return []

    xb = _cluster([r[0] for r in v_grid])
    yb = _cluster([r[0] for r in h_grid])
    if (len(xb) < MIN_COLS + 1 or len(yb) < MIN_ROWS + 1
            or len(xb) > MAX_COLS + 1 or len(yb) > MAX_ROWS + 1):
        return []

    nrows, ncols = len(yb) - 1, len(xb) - 1
    texts = _texts_by_cell(page, xb, yb)
    if not texts:
        return []

    # Объединённые ячейки: нет внутренней линии → слито с верхней/левой
    merged_v = [[False] * (ncols + 1) for _ in range(nrows)]
    for ri in range(nrows):
        for ci in range(1, ncols):
            merged_v[ri][ci] = not _has_v_rule(v_rules, xb[ci], yb[ri], yb[ri + 1])
    merged_h = [[False] * ncols for _ in range(nrows + 1)]
    for ci in range(ncols):
        for ri in range(1, nrows):
            merged_h[ri][ci] = not _has_h_rule(h_rules, yb[ri], xb[ci], xb[ci + 1])

    grid: List[List[str]] = [["" for _ in range(ncols)] for _ in range(nrows)]
    for (ri, ci), text in texts.items():
        while ri > 0 and merged_h[ri][ci]:      # уводим текст в верхнюю строку слияния
            ri -= 1
        while ci > 0 and merged_v[ri][ci]:      # и в левую колонку слияния
            ci -= 1
        if not grid[ri][ci]:
            grid[ri][ci] = text

    keep_rows = [i for i in range(nrows) if any(c.strip() for c in grid[i])]
    keep_cols = [j for j in range(ncols) if any(grid[i][j].strip() for i in range(nrows))]
    if len(keep_rows) < MIN_ROWS or len(keep_cols) < MIN_COLS:
        return []
    data = [[grid[i][j] for j in keep_cols] for i in keep_rows]

    merged = sum(sum(1 for f in row if f) for row in merged_v) + \
        sum(sum(1 for f in row if f) for row in merged_h)
    bbox = [min(xb), min(yb), max(xb), max(yb)]
    logger.info(f"[table-rules] сетка {len(data)}x{len(data[0])}: правил "
                f"{len(h_rules)}x{len(v_rules)}, слияний {merged}")
    return [{"rows": data, "merged_cells": merged, "bbox": bbox}]


def tables_to_payload(page, tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Дополнить сетки до формата find_tables-пути: markdown, html, шапка, качество."""
    from src.indexing.table_ids import table_quality, table_stats

    out: List[Dict[str, Any]] = []
    for tb in tables:
        data = tb["rows"]
        md = ["| " + " | ".join(str(c or "").replace("\n", " ") for c in data[0]) + " |",
              "|" + "|".join("---" for _ in data[0]) + "|"]
        for row in data[1:]:
            md.append("| " + " | ".join(str(c or "").replace("\n", "<br>") for c in row) + " |")
        html = ["<table>"]
        for ri, row in enumerate(data):
            tag = "th" if ri == 0 else "td"
            html.append("<tr>" + "".join(
                f"<{tag}>{str(c or '').replace(chr(10), '<br>')}</{tag}>" for c in row) + "</tr>")
        html.append("</table>")
        col_counts = {len(r) for r in data}
        empty_header = any(not str(c).strip() for c in data[0])
        out.append({
            "markdown": "\n".join(md),
            "html": "\n".join(html),
            "text": "\n".join(" | ".join(str(c or "") for c in r) for r in data),
            "rows": data,
            "headers": [str(c or "").replace("\n", " ") for c in data[0]],
            "bbox": [round(v, 1) for v in tb.get("bbox", [])],
            "complex": bool(len(col_counts) > 1 or empty_header or len(data[0]) > 8
                            or tb.get("merged_cells")),
            **table_stats(data),
            "quality": table_quality(data),
            "extraction_method": "pymupdf-rules",
        })
    return out
