"""Тесты восстановления таблиц: сетка + текст, разбор markdown от модели, сетка по цветным заливкам."""
from __future__ import annotations

import numpy as np
from PIL import Image

from src.indexing.table_recovery import (
    OcrLine,
    SOURCE_VLM,
    cells_from_grid,
    cells_from_grid_annotated,
    grid_from_color_blocks,
    table_from_markdown,
)

# Сетка как её отдаёт Occular: диапазоны координат строк и колонок
ROWS = [(0.0, 20.0), (20.0, 40.0), (40.0, 60.0)]
COLS = [(0.0, 100.0), (100.0, 200.0), (200.0, 300.0)]


def test_cells_from_grid_places_text_by_center():
    lines = [
        OcrLine("Наименование", (5, 5, 90, 15)),
        OcrLine("Кол", (150, 5, 190, 15)),
        OcrLine("Блок детектирования", (5, 25, 95, 35)),
        OcrLine("4", (150, 25, 160, 35)),
    ]
    grid = cells_from_grid(ROWS, COLS, lines)
    assert grid[0] == ["Наименование", "Кол", ""]
    assert grid[1] == ["Блок детектирования", "4", ""]
    assert grid[2] == ["", "", ""]


def test_cells_from_grid_joins_multiline_cell():
    """В ячейке текст переносится по строкам — строки соединяем, иначе потеряем половину."""
    lines = [
        OcrLine("Контроль мощности", (110, 25, 195, 33)),
        OcrLine("поглощенной дозы", (110, 33, 190, 40)),
    ]
    grid = cells_from_grid(ROWS, COLS, lines)
    assert grid[1][1] == "Контроль мощности поглощенной дозы"


def test_cells_from_grid_marks_merged_cells():
    """Растянутая строка помечается: объединённые ячейки мы пока не восстанавливаем."""
    lines = [OcrLine("Итого по всем блокам сразу", (5, 45, 280, 58))]
    grid, notes = cells_from_grid_annotated(ROWS, COLS, lines)
    assert grid[2][1] == "Итого по всем блокам сразу"   # строка растянута, центр попал в колонку 1
    assert any("объединённую ячейку" in n for n in notes)


def test_cells_from_grid_empty_grid_is_reported():
    grid, notes = cells_from_grid_annotated([], [], [OcrLine("текст", (0, 0, 1, 1))])
    assert grid == []
    assert notes and "пустая сетка" in notes[0]


def test_table_from_markdown_parses_model_answer():
    """Ровно тот ответ, который дала Qwen2-VL по файлу владельца (числа восстановлены)."""
    md = (
        "| Наименование устройства, блока | Назначение устройства, блока | Количество | Примечание |\n"
        "|---|---|---|---|\n"
        "| Блок детектирования | Контроль мощности поглощенной дозы | 4 | В защитном кожухе |\n"
        "| Устройство обработки | Обработка данных от блоков | 28 | |\n"
    )
    table = table_from_markdown(md, source_model="qwen2vl:2b", page=7)
    assert table is not None
    assert table.source == SOURCE_VLM
    assert table.source_model == "qwen2vl:2b"
    assert table.page == 7
    assert table.n_rows == 3          # шапка + две строки, разделитель выброшен
    assert table.n_cols == 4
    assert table.rows[1][2] == "4"
    assert table.rows[2][2] == "28"
    assert table.is_usable()


def test_table_from_markdown_ignores_surrounding_talk():
    """Модель иногда добавляет пояснения до и после — их не берём, берём самый большой блок."""
    md = ("Вот таблица:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\nНадеюсь, помог.")
    table = table_from_markdown(md)
    assert table is not None and table.rows == [["a", "b"], ["1", "2"], ["3", "4"]]


def test_table_from_markdown_returns_none_for_plain_text():
    assert table_from_markdown("просто текст без таблицы") is None


def _page_with_colored_cells() -> Image.Image:
    """Синтетическая таблица без линий: 3 строки × 2 колонки, заливки разных цветов."""
    img = Image.new("RGB", (200, 120), (255, 255, 255))
    colors = [(200, 220, 255), (210, 240, 210), (250, 230, 200)]
    for r, color in enumerate(colors):
        for c in range(2):
            for y in range(r * 40, r * 40 + 40):
                for x in range(c * 100, c * 100 + 100):
                    img.putpixel((x, y), color)
    return img


def test_grid_from_color_blocks_finds_lineless_table():
    rows, cols = grid_from_color_blocks(_page_with_colored_cells())
    assert rows is not None and cols is not None
    assert len(rows) == 3, f"строк должно быть 3, получили {len(rows)}"
    assert len(cols) == 2, f"колонок должно быть 2, получили {len(cols)}"


def test_grid_from_color_blocks_ignores_plain_page():
    plain = Image.new("RGB", (200, 120), (255, 255, 255))
    rows, cols = grid_from_color_blocks(plain)
    assert (rows, cols) == (None, None)


def test_grid_from_color_blocks_reads_file_and_bytes(tmp_path):
    path = tmp_path / "page.png"
    _page_with_colored_cells().save(path)
    rows, cols = grid_from_color_blocks(str(path))
    assert rows is not None and len(cols) == 2
    rows2, cols2 = grid_from_color_blocks(path.read_bytes())
    assert rows2 is not None and len(cols2) == 2
    assert np.asarray(_page_with_colored_cells()).shape[1] == 200
