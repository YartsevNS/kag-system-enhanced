"""Сетка таблицы из векторной графики PDF — тесты на синтетических страницах.

Проверяем то, ради чего модуль написан: сетка берётся из НАРИСОВАННЫХ ячеек, а не из
эвристики по тексту, и объединённая ячейка не дублирует своё значение в каждой строке.
"""
import fitz

from src.indexing.table_rects import extract_tables_from_vectors, tables_to_payload

CELL_W, CELL_H = 100, 40
X0, Y0 = 50, 50


def _page(rows, cols, texts, merges=()):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    covered = set()
    for (r, c, rspan, cspan) in merges:
        rect = fitz.Rect(X0 + c * CELL_W, Y0 + r * CELL_H,
                         X0 + (c + cspan) * CELL_W, Y0 + (r + rspan) * CELL_H)
        page.draw_rect(rect, color=(0, 0, 0), width=0.7)
        for rr in range(r, r + rspan):
            for cc in range(c, c + cspan):
                covered.add((rr, cc))
    for r in range(rows):
        for c in range(cols):
            if (r, c) in covered:
                continue
            page.draw_rect(fitz.Rect(X0 + c * CELL_W, Y0 + r * CELL_H,
                                     X0 + (c + 1) * CELL_W, Y0 + (r + 1) * CELL_H),
                           color=(0, 0, 0), width=0.7)
    for (r, c), text in texts.items():
        page.insert_text((X0 + c * CELL_W + 6, Y0 + r * CELL_H + CELL_H * 0.6), text, fontsize=11)
    return page


def test_grid_from_rectangles():
    page = _page(3, 2, {(0, 0): "Stage", (0, 1): "Action",
                        (1, 0): "First", (1, 1): "Writes",
                        (2, 0): "Second", (2, 1): "Speaks"})
    tables = tables_to_payload(page, extract_tables_from_vectors(page))
    assert len(tables) == 1, "сетка должна восстановиться как одна таблица"
    rows = tables[0]["rows"]
    assert len(rows) == 3 and all(len(r) == 2 for r in rows)
    assert rows[0][0] == "Stage" and rows[0][1] == "Action"
    assert rows[1][0] == "First" and rows[2][1] == "Speaks"
    assert tables[0]["extraction_method"] == "pymupdf-rules"


def test_merged_cell_not_duplicated():
    page = _page(3, 2, {(0, 0): "Head1", (0, 1): "Head2", (1, 1): "one", (2, 1): "two"},
                 merges=[(1, 0, 2, 1)])
    page.insert_text((X0 + 6, Y0 + CELL_H + CELL_H * 0.6), "Single stage", fontsize=11)
    tables = tables_to_payload(page, extract_tables_from_vectors(page))
    assert tables, "таблица должна найтись"
    rows = tables[0]["rows"]
    assert rows[1][0].startswith("Single stage")
    assert not rows[2][0].strip(), "объединённая ячейка не должна дублироваться в следующей строке"
    assert tables[0]["complex"] is True, "объединённые ячейки помечаются как сложная таблица"


def test_wrapped_text_in_one_cell_is_joined():
    page = _page(2, 2, {(0, 0): "Header", (0, 1): "Value", (1, 1): "one"})
    page.insert_text((X0 + 6, Y0 + CELL_H + CELL_H * 0.5), "first part", fontsize=11)
    page.insert_text((X0 + 6, Y0 + CELL_H + CELL_H * 0.5 + 12), "second part", fontsize=11)
    tables = tables_to_payload(page, extract_tables_from_vectors(page))
    cell = tables[0]["rows"][1][0]
    assert "first part" in cell and "second part" in cell


def test_no_vectors_no_table():
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((72, 100), "Просто текст без рамок", fontsize=12)
    assert extract_tables_from_vectors(page) == []
