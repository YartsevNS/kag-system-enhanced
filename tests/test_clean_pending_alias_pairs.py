"""Тесты классификатора pending-пар алиасов (решение владельца: «точно не совпадают → не сравнивать»)."""
from scripts.clean_pending_alias_pairs import classify, is_true_alias


def test_true_alias_substring():
    """Полное/сокращённое: подстрока — это алиас."""
    assert classify("ПАО Сбербанк", "Сбербанк") == "approved"
    assert classify("Банк России", "Центральный банк Российской Федерации") in ("approved", "rejected")
    # «Центральный банк РФ» ⊃ «Банк России» — подстроки нет, но полное/сокращение близко;
    # главное — НЕ «exact»-мусор


def test_true_alias_initials():
    """Инициалы по словам: «ЦБ РФ» = «Центральный банк Российской Федерации»."""
    assert is_true_alias("Центральный банк Российской Федерации", "ЦБ РФ") is True
    assert classify("Центральный банк Российской Федерации", "ЦБ РФ") == "approved"


def test_acronym_in_word_not_alias():
    """Акроним в одном слове («РСХБ» ⊂ «Россельхозбанк») — НЕ склеиваем (ложные «Банк»/«банков»)."""
    assert is_true_alias("Россельхозбанк", "РСХБ") is False
    assert classify("Банк России", "Банк") == "rejected"


def test_category_is_not_alias():
    """Термин ↔ категория — НЕ алиас («Банк России» ≠ «кредитные организации»)."""
    assert classify("Банк России", "кредитные организации") == "rejected"
    assert is_true_alias("Банк России", "кредитные организации") is False


def test_date_vs_year():
    """Дата и год — НЕ алиас: «2008-02-01» ≠ «2008»."""
    assert classify("2008-02-01", "2008") == "rejected"


def test_exact_collision():
    """Точное совпадение после нормализации → exact."""
    assert classify(" Тест  ", "тест") == "exact"


def test_short_names_rejected():
    """Слишком короткие имена (<4) — не алиас (защита от ложных склей)."""
    assert classify("АБ", "Аб") == "rejected"
