"""Тесты арифметической проверки таблиц УПД/накладных."""
from src.indexing.table_validate import check_table, detect_columns, parse_amount

HEADER = ["Наименование товара", "Код вида товара", "Единица измерения", "Количество",
          "Цена (тариф) за единицу", "Стоимость товаров", "В том числе сумма акциза",
          "Налоговая ставка", "Сумма налога", "Стоимость товаров с налогом"]


def _row(name, qty, price, amount, rate="20%", tax=None, total=None):
    """Строка таблицы: налог и стоимость с налогом считаются от суммы, если не заданы явно."""
    a = parse_amount(amount)
    tax = round(a * 0.2 / 1.2, 2) if tax is None else tax
    total = round(a + tax, 2) if total is None else total
    num = lambda v: (("%.2f" % v).replace(".", ",")) if isinstance(v, float) else str(v)  # noqa: E731
    return [name, "", "шт", num(qty), num(price), num(amount), "", rate, num(tax), num(total)]


def test_parse_amount_russian_formats():
    assert parse_amount("13 959,9") == 13959.9
    assert parse_amount("2 791.98") == 2791.98
    assert parse_amount("43 250,36") == 43250.36
    assert parse_amount("796") == 796.0
    assert parse_amount("1 873,3") == 1873.3
    assert parse_amount("\u00a02 512,30") == 2512.30
    assert parse_amount("Без акциза") is None
    assert parse_amount("") is None
    assert parse_amount(None) is None


def test_detect_columns_from_header():
    cols = detect_columns([HEADER])
    assert cols.get("qty") == 3
    assert cols.get("price") == 4
    assert cols.get("amount") == 5
    assert cols.get("rate") == 7
    assert cols.get("tax") == 8
    assert cols.get("total") == 9


def _row_check(verdict, index):
    return next((r for r in verdict.rows if r.index == index), None)


def test_clean_rows_pass():
    rows = [HEADER,
            _row("Балка MS Pro 150 b", 796, 6.54, 5205.84),
            _row("Настил для полки", 796, 1793.69, 1427777.24)]
    verdict = check_table(rows)
    assert verdict.checked == 2, f"проверено строк: {verdict.checked}"
    assert verdict.mismatch == 0, f"расхождения там, где их нет: {verdict.as_dict()}"
    assert verdict.verdict == "строки сходятся, итоги не найдены"


def test_wrong_amount_is_caught():
    """Ошибка распознавания в сумме (1↔7, 3↔8) должна быть поймана."""
    bad = _row("Балка MS Pro 150 b", 796, 6.54, 5205.84)
    bad[5] = "5 805,84"                      # подменили сумму
    verdict = check_table([HEADER, bad])
    assert verdict.mismatch == 1
    check = _row_check(verdict, 1)
    assert check and any("кол-во × цена" in d for d in check.details), verdict.as_dict()
    assert "расхождений: 1" in verdict.verdict


def test_shifted_column_is_caught():
    """Сдвиг значений на колонку (частая ошибка раскладки) ловится арифметикой.

    Сдвигаем так, чтобы проверка не оказалась случайно верной: сумма уезжает в колонку количества.
    """
    row = _row("Настил", 796, 3134.72, 2495241.5)
    shifted = row[:]
    shifted[3] = row[5]          # в количество попала сумма
    verdict = check_table([HEADER, shifted])
    assert verdict.mismatch == 1, verdict.as_dict()


def test_consistent_row_passes_and_real_noisy_row_is_flagged():
    """Согласованная строка проходит; строка с числами из реальной накладной — нет.

    В разборе скана накладной у строки стоят «796» (количество), «6,54» (цена) и «2 791.98» (стоимость):
    796 × 6,54 = 5 205,84. Значит либо значения съехали по колонкам, либо цифры распознаны неверно — именно это
    и должен ловить валидатор, в отличие от структурных метрик (TEDS, заполненность ячеек).
    """
    assert check_table([HEADER, _row("Балка MS Pro 150 b", 796, 6.54, 5205.84)]).mismatch == 0

    verdict = check_table([HEADER, _row("Балка MS Pro 150 b", 796, "6,54", "2 791.98")])
    assert verdict.mismatch == 1, "несогласованная строка не поймана"
    check = _row_check(verdict, 1)
    assert check and "5205" in check.details[0].replace(".", "").replace(",", ".")


def test_totals_row_checked():
    """Строка «Итого» сверяется с суммой строк по тем же колонкам, что и строки."""
    good = [HEADER,
            _row("Балка", 10, 100.0, 1000.0),
            _row("Настил", 20, 50.0, 1000.0),
            ["Итого", "", "", "", "", "2 000,00", "", "", "333,33", "2 333,33"]]
    verdict = check_table(good)
    assert verdict.totals_checked and verdict.totals_ok is True, verdict.as_dict()

    bad = [row[:] for row in good]
    bad[-1][5] = "3 000,00"
    verdict2 = check_table(bad)
    assert verdict2.totals_ok is False
    assert any("сумма строк" in n for n in verdict2.notes)


def test_no_arithmetic_columns_reports_honestly():
    verdict = check_table([["Наименование", "Код"], ["Балка", "531299000402"]])
    assert verdict.checked == 0
    assert "не найдены колонки" in verdict.notes[0]
    assert verdict.verdict == "нет данных для проверки"
