"""Тесты признака «числовая / прозаическая таблица» и склейки строк внутри ячейки.

Признак калиброван замером 04.10.2026 (смета: 2 числовых столбца из 5; конспект: 0 из 4), поэтому
тесты фиксируют ровно то поведение, на которое мы опираемся, и границы: одиночный числовой столбец,
пустой вход, блоки без координат.
"""
from __future__ import annotations

from src.indexing import table_type_route as route_mod


def line(text: str, x0: float, y0: float, x1: float, y1: float) -> dict:
    return {"text": text, "bbox": [x0, y0, x1, y1]}


def numeric_table() -> list:
    """Смета: «№» (числа), «наименование» (текст), «стоимость» (суммы)."""
    lines = []
    for i in range(8):
        y = 100 + i * 40
        lines.append(line(str(i + 1), 90, y, 110, y + 25))
        lines.append(line(f"Работа номер {i + 1}", 250, y, 500, y + 25))
        lines.append(line(f"{1000 + i * 100},00", 650, y, 750, y + 25))
    return lines


def prose_table() -> list:
    """Конспект: три текстовых столбца, ни одного числа."""
    lines = []
    for i in range(10):
        y = 100 + i * 40
        lines.append(line(f"Этап {i + 1}", 90, y, 190, y + 25))
        lines.append(line(f"Учитель объясняет тему {i + 1}", 250, y, 350, y + 25))
        lines.append(line(f"Ученики выполняют задание {i + 1}", 650, y, 750, y + 25))
    return lines


# ── распознавание чисел ────────────────────────────────────────────────────
def test_is_number_recognises_numbers_sums_dates_and_percent():
    assert route_mod.is_number("4500")
    assert route_mod.is_number("4 500,00")
    assert route_mod.is_number("1\u00a0234,56")      # неразрывный пробел
    assert route_mod.is_number("-12.5")
    assert route_mod.is_number("09.11.2020")         # дата
    assert route_mod.is_number("12%")
    assert not route_mod.is_number("")


def test_is_number_rejects_text():
    for value in ("Наименование работ", "ООО «Метбокс»", "шт", "1 шт", "№1", "с2000-2"):
        assert not route_mod.is_number(value), value


# ── признак типа ───────────────────────────────────────────────────────────
def test_numeric_table_is_routed_to_numeric():
    result = route_mod.route(numeric_table())
    assert result["kind"] == "numeric"
    assert result["numeric_columns"] >= 2
    assert "числовых" in result["reason"]


def test_prose_table_is_routed_to_prose():
    result = route_mod.route(prose_table())
    assert result["kind"] == "prose"
    assert result["numeric_columns"] == 0


def test_single_numeric_column_is_not_enough():
    """Правило требует ДВА числовых столбца: одна колонка чисел бывает и в прозаической таблице."""
    lines = numeric_table() + [line("Комментарий", 850, 100 + i * 40, 950, 125 + i * 40) for i in range(8)]
    only_amount = [l for l in lines if not (90 <= l["bbox"][0] < 250)]          # убрали колонку «№»
    assert route_mod.route(only_amount)["kind"] == "prose"
    assert route_mod.route(lines)["kind"] == "numeric"


def test_empty_input_is_prose_with_reason():
    result = route_mod.route([])
    assert result["kind"] == "prose" and result["blocks"] == 0
    assert "нечем" in result["reason"]


def test_lines_without_coordinates_are_ignored():
    result = route_mod.route([{"text": "1"}, {"text": "2"}, {"text": "текст"}])
    assert result["blocks"] == 0 and result["kind"] == "prose"


def test_quad_lines_are_supported():
    """Строки OCR приходят с quad (4 точки) — признак должен их понимать."""
    lines = []
    for i in range(6):
        y = 100 + i * 40
        lines.append({"text": str(i + 1), "quad": [[90, y], [110, y], [110, y + 25], [90, y + 25]]})
        lines.append({"text": f"{500 + i},00", "quad": [[650, y], [750, y], [750, y + 25], [650, y + 25]]})
        lines.append({"text": "работа", "quad": [[250, y], [500, y], [500, y + 25], [250, y + 25]]})
    assert route_mod.route(lines)["kind"] == "numeric"


# ── склейка строк внутри ячейки ────────────────────────────────────────────
def test_merge_cell_lines_joins_all_lines_in_reading_order():
    """Ключевое отличие от «одного блока с максимальным перекрытием»: берём ВСЕ строки ячейки."""
    cell = (0.0, 0.0, 100.0, 100.0)
    lines = [
        line("третья", 10, 70, 60, 85),
        line("первая", 10, 10, 60, 25),
        line("вторая", 10, 40, 80, 55),
        line("снаружи", 500, 500, 600, 600),
    ]
    assert route_mod.merge_cell_lines(cell, lines) == "первая вторая третья"


def test_merge_cell_lines_reads_left_to_right_within_a_row():
    cell = (0.0, 0.0, 200.0, 60.0)
    lines = [line("второй", 110, 10, 190, 30), line("первый", 10, 12, 90, 32)]
    assert route_mod.merge_cell_lines(cell, lines) == "первый второй"


def test_merge_cell_lines_counts_line_overlapping_cell_mostly():
    """Строка, чей центр вне ячейки, но которая перекрывает её больше половины, в ячейку попадает."""
    cell = (0.0, 0.0, 100.0, 100.0)
    lines = [line("заходящая", 60, -30, 140, -10)]     # центр (100, -20) — вне; перекрытие 40 из 80 = 0.5
    assert route_mod.merge_cell_lines(cell, lines) == "заходящая"


def test_merge_cells_walks_all_boxes():
    lines = [line("a", 5, 5, 20, 20), line("b", 105, 5, 120, 20)]
    assert route_mod.merge_cells([(0, 0, 100, 100), (100, 0, 200, 100)], lines) == ["a", "b"]


def test_status_exposes_thresholds():
    st = route_mod.status()
    assert st["numeric_columns_needed"] == 2
    assert 0 < st["numeric_column_share"] <= 1
