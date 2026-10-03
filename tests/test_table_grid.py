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
    assert "796 шт 796 шт" in table.rows[1]              # данные не потеряны (колонка — по перекрытию)
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


def test_split_uses_all_boundaries_when_text_has_spaces():
    """Предела «не больше N колонок» больше нет: строка с пробелами режется по всем границам.

    Ограничение теперь не по числу колонок, а по смыслу: режем только там, где в тексте пробел. Так строка
    заголовка с пробелами корректно раскладывается по колонкам, а число, разрезанное линией, — нет.
    """
    cut = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0]
    grid = Grid(rows=[(10.0, 60.0)], row_lines=[cut], lines_h=[10.0, 60.0], lines_v=cut,
                bbox=(0.0, 10.0, 700.0, 60.0))
    lines = [{"text": "а б в г д е ж", "quad": [[0, 20], [700, 20], [700, 50], [0, 50]]}]
    seen_quads = []

    def fake_recognize(image, quads):
        seen_quads.extend(quads)
        return [(f"к{len(seen_quads)}", 0.9) for _ in quads]

    table = fill_cells(None, grid, lines, recognize_cells=fake_recognize)
    assert len(seen_quads) == 7, f"ожидалось 7 кусков, получено {len(seen_quads)}"
    assert all(table.rows[0])                        # каждая колонка заполнена


def test_boundary_inside_number_does_not_split():
    """Граница внутри числа не режет: «18 ^ 06.652» остаётся одной ячейкой (целиком в колонке перекрытия)."""
    cut = [0.0, 100.0, 200.0]
    grid = Grid(rows=[(10.0, 60.0)], row_lines=[cut], lines_h=[10.0, 60.0], lines_v=cut,
                bbox=(0.0, 10.0, 200.0, 60.0))
    line = {"text": "18.652", "quad": [[40, 20], [160, 20], [160, 50], [40, 50]]}
    calls = []

    def fake_recognize(image, quads):
        calls.extend(quads)
        return [("не должно вызываться", 0.5)]

    table = fill_cells(None, grid, [line], recognize_cells=fake_recognize)
    assert not calls, "число разрезано, хотя граница попала внутрь него"
    assert "18.652" in table.rows[0]



def test_tables_not_in_fragments_by_default():
    """Таблицы со сканов по умолчанию не идут во фрагменты (иначе в поиск и «Чанки» попадает каша)."""
    from src.indexing.tables_settings import DEFAULTS, tables_in_fragments

    assert DEFAULTS.get("tables_in_fragments") is False
    assert tables_in_fragments() is False


def test_refine_cells_reads_each_cell_by_its_own_crop():
    """Дочитывание ячеек: текст каждой ячейки берётся из её вырезки — так к ячейкам применяется свой язык.

    Проверяем главное: запросы идут по bbox ЯЧЕЕК сетки (а не по строкам), и ответ подставляется на место.
    """
    from src.indexing.table_grid import Grid, refine_table_cells
    from src.indexing.table_recovery import RecoveredTable

    grid = Grid(rows=[(10.0, 40.0), (40.0, 70.0)],
                row_lines=[[0.0, 100.0, 200.0], [0.0, 100.0, 200.0]],
                lines_h=[10.0, 40.0, 70.0], lines_v=[0.0, 100.0, 200.0],
                bbox=(0.0, 10.0, 200.0, 70.0))
    table = RecoveredTable(rows=[["мусор", "мусор"], ["мусор", "мусор"]], source="occular-grid")
    seen = []

    def fake_recognize(image, quads):
        for q in quads:
            seen.append((float(q[:, 0].min()), float(q[:, 0].max())))
        return [("13 959,9", 0.9), ("796", 0.9), ("2", 0.9), ("шт", 0.9)]

    changed = refine_table_cells(None, grid, table, fake_recognize)
    assert changed == 4
    assert len(seen) == 4, "по одной вырезке на каждую ячейку сетки"
    assert table.rows[0][0] == "13 959,9" and table.rows[0][1] == "796"
