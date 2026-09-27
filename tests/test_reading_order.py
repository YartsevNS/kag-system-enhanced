"""Тесты сборки текста страницы по порядку чтения (геометрия, без модели)."""
from src.indexing.reading_order import find_gutters, order_page


def _line(text, x0, y0, x1, y1):
    return {"text": text, "quad": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def test_single_column_sorted_top_down():
    """Строки приходят вразнобой — текст собирается сверху вниз."""
    lines = [_line("третья строка", 20, 200, 400, 220),
             _line("первая строка", 20, 100, 400, 120),
             _line("вторая строка", 20, 150, 400, 170)]
    page = order_page(lines)
    assert page.text.startswith("первая строка")
    assert page.text.index("первая") < page.text.index("вторая") < page.text.index("третья")


def test_table_row_joined_left_to_right():
    """Ячейки одной высоты — одна полоса, читаем слева направо и разделяем вертикальной чертой."""
    lines = [_line("13 959,9", 700, 300, 800, 320),
             _line("796", 500, 300, 560, 320),
             _line("шт", 570, 300, 600, 320),
             _line("6,54", 610, 300, 690, 320)]
    page = order_page(lines)
    assert page.blocks, "должен получиться хотя бы один блок"
    block = page.blocks[0]
    assert block.is_table_row, "полоса из четырёх числовых ячеек — строка таблицы"
    assert block.text.index("796") < block.text.index("шт") < block.text.index("6,54") < block.text.index("13 959,9")


def test_two_columns_read_left_then_right():
    """Многоколоночная страница: левая колонка читается целиком, затем правая."""
    lines = []
    for i in range(4):
        lines.append(_line(f"левая {i}", 40, 100 + i * 40, 400, 120 + i * 40))
        lines.append(_line(f"правая {i}", 600, 100 + i * 40, 900, 120 + i * 40))
    page = order_page(lines)
    assert page.n_blocks >= 2, "колонки должны дать отдельные блоки"
    text = page.text
    assert text.index("левая 3") < text.index("правая 0"), "левая колонка должна быть прочитана раньше правой"


def test_gutter_found_between_columns():
    """Коридор между колонками находится; в одноколоночном тексте коридоров нет."""
    two = []
    for i in range(5):
        two.append(_line("a", 40, 50 + i * 30, 450, 70 + i * 30))
        two.append(_line("b", 520, 50 + i * 30, 950, 70 + i * 30))
    assert find_gutters([(l["quad"][0][0], l["quad"][0][1], l["quad"][2][0], l["quad"][2][1]) for l in two],
                        1000, 400), "коридор между колонками должен найтись"
    one = [(50, 50 + i * 40, 950, 70 + i * 40) for i in range(5)]
    assert find_gutters(one, 1000, 400) == [], "в одноколоночном тексте коридоров быть не должно"


def test_hyphen_joined():
    """Перенос слова склеивается без дефиса."""
    lines = [_line("цио-", 20, 100, 200, 120), _line("производимости", 20, 125, 300, 145)]
    page = order_page(lines)
    assert "циопроизводимости" in page.text


def test_big_gap_splits_paragraphs():
    """Большой разрыв по вертикали — новый абзац (новый блок)."""
    lines = [_line("первый абзац", 20, 100, 400, 120),
             _line("продолжение", 20, 130, 400, 150),
             _line("далеко внизу", 20, 500, 400, 520)]
    page = order_page(lines)
    assert page.n_blocks >= 2, "разрыв должен разделить блоки"
    assert "первый абзац продолжение" == page.blocks[0].text
    assert page.blocks[-1].text == "далеко внизу"


def test_empty_input_is_safe():
    page = order_page([])
    assert page.text == "" and page.n_blocks == 0
    assert page.notes, "причина должна быть указана"
