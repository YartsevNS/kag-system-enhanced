"""Тесты сетки таблицы по линиям и раскладки ячеек (без модели распознавания — она подменяется)."""
from __future__ import annotations

import numpy as np

from src.indexing.table_grid import Grid, fill_cells, table_quality


def _grid():
    # 3 строки, 3 колонки: линии по x = 0, 100, 200, 300; по y = 0, 20, 40, 60
    rows = [(0.0, 20.0), (20.0, 40.0), (40.0, 60.0)]
    row_lines = [[0.0, 100.0, 200.0, 300.0]] * 3
    return Grid(rows=rows, row_lines=row_lines, lines_h=[0.0, 20.0, 40.0, 60.0],
                lines_v=[0.0, 100.0, 200.0, 300.0], bbox=(0.0, 0.0, 300.0, 60.0))


def test_line_inside_one_column_goes_to_its_cell():
    lines = [
        {"text": "Наименование", "quad": [[5, 5], [95, 5], [95, 15], [5, 15]]},
        {"text": "Кол", "quad": [[105, 5], [195, 5], [195, 15], [105, 15]]},
        {"text": "Балка", "quad": [[5, 25], [95, 25], [95, 35], [5, 35]]},
        {"text": "4", "quad": [[105, 25], [115, 25], [115, 35], [105, 35]]},
    ]
    table = fill_cells(object(), _grid(), lines, recognize_cells=None)
    assert table.rows[0][:3] == ["Наименование", "Кол", ""]
    assert table.rows[1][:3] == ["Балка", "4", ""]


def test_wide_line_is_split_across_columns_and_recognized_per_part():
    """Строка, накрывающая три колонки («796 шт 796 шт 796 шт»), режется по линиям колонок."""
    seen = {}

    def fake_recognize(image, quads):
        seen["count"] = len(quads)
        # возвращаем текст по центру каждого куска, чтобы проверить попадание в колонку
        return [(f"часть{int((np.asarray(q)[:, 0].min()) // 100)}", 0.9) for q in quads]

    lines = [{"text": "796 шт 796 шт 796 шт", "quad": [[5, 25], [295, 25], [295, 35], [5, 35]]}]
    table = fill_cells(object(), _grid(), lines, recognize_cells=fake_recognize)
    assert seen["count"] == 3, "строка должна быть разрезана на три куска"
    assert table.rows[1][0] == "часть0"
    assert table.rows[1][1] == "часть1"
    assert table.rows[1][2] == "часть2"


def test_without_recognizer_wide_line_keeps_text_in_center_column():
    lines = [{"text": "796 шт 796 шт", "quad": [[5, 25], [295, 25], [295, 35], [5, 35]]}]
    table = fill_cells(object(), _grid(), lines, recognize_cells=None)
    assert table.rows[1][0] == "796 шт 796 шт"          # данные не потеряны
    assert "разрезано" not in " ".join(table.notes)


def test_multiline_cell_text_is_joined():
    lines = [
        {"text": "Контроль мощности", "quad": [[5, 25], [95, 25], [95, 30], [5, 30]]},
        {"text": "поглощенной дозы", "quad": [[5, 31], [95, 31], [95, 36], [5, 36]]},
    ]
    table = fill_cells(object(), _grid(), lines, recognize_cells=None)
    assert table.rows[1][0] == "Контроль мощности поглощенной дозы"


def test_table_quality_separates_empty_from_real():
    assert table_quality([]) == 0.0
    assert table_quality([["", "", ""], ["", "", ""]]) == 0.0
    real = table_quality([["Наименование", "Кол", "Цена"], ["Балка", "2", "1344,42"]])
    assert real > 0.7, real
    garbage = table_quality([["", ".", ""], ["", "", "*"]])
    assert garbage < 0.3, garbage


def test_split_uses_full_document_column_set():
    """В строке без внутренних линий ячейка широкая — режем по полному набору колонок документа.

    Так было на накладной: в строке с позициями детектор сливал «796 шт 796 шт» в одну распознанную строку,
    и без полного набора колонок значения оставались в одной ячейке.
    """
    # в этой строке нарисованы только две линии: 0 и 300 — то есть одна «объединённая» ячейка
    grid = Grid(rows=[(0.0, 20.0)], row_lines=[[0.0, 300.0]], lines_h=[0.0, 20.0],
                lines_v=[0.0, 100.0, 200.0, 300.0], bbox=(0.0, 0.0, 300.0, 20.0))
    cut = [0.0, 100.0, 200.0, 300.0]

    def fake_recognize(image, quads):
        return [(f"часть{idx}", 0.9) for idx, _ in enumerate(quads)]

    lines = [{"text": "796 шт 796 шт 796 шт", "quad": [[5, 5], [295, 5], [295, 15], [5, 15]]}]
    table = fill_cells(object(), grid, lines, recognize_cells=fake_recognize, split_lines=cut)
    assert table.rows[0][0] == "часть0"
    assert table.rows[0][1] == "часть1"
    assert table.rows[0][2] == "часть2"


def test_split_capped_for_very_wide_lines():
    """Слишком широкую строку (больше предела колонок) не режем — иначе теряем текст."""
    grid = Grid(rows=[(0.0, 20.0)], row_lines=[[0.0, 800.0]], lines_h=[0.0, 20.0],
                lines_v=[float(x) for x in range(0, 900, 100)], bbox=(0.0, 0.0, 800.0, 20.0))
    lines = [{"text": "заголовок на всю таблицу", "quad": [[5, 5], [795, 5], [795, 15], [5, 15]]}]
    table = fill_cells(object(), grid, lines, recognize_cells=lambda i, q: [("x", 1.0)] * len(q),
                       split_lines=[float(x) for x in range(0, 900, 100)])
    assert table.rows[0][0] == "заголовок на всю таблицу"
