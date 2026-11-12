"""Правила автопилота и покрытие вопроса — чистые функции, без БД и стенда.

Зачем тесты именно здесь: правила принимают решения без человека, поэтому
у каждого должно быть закреплено ожидаемое поведение. Ошибка в правиле
подтверждает мусор молча — это худший вид дефекта в онтологии.
"""
from src.api.services import answerability, automation


def test_same_letters_is_auto():
    verdict, rule, _ = automation.alias_decision("САРЕС", "сарес")
    assert verdict == "auto" and rule == "same_letters"


def test_registry_entry_without_canon_is_auto():
    verdict, rule, _ = automation.alias_decision("Антивирусная защита", "")
    assert verdict == "auto" and rule == "registry_entry"


def test_requisites_is_auto():
    verdict, rule, _ = automation.alias_decision("ООО «Ромашка», ИНН 7701234567", "Ромашка")
    assert verdict == "auto" and rule == "requisites"


def test_abbreviation_from_full_is_auto():
    # ФСТЭК ← Федеральная служба по техническому и экспортному контролю
    verdict, rule, _ = automation.alias_decision(
        "ФСТЭК", "Федеральная служба по техническому и экспортному контролю")
    assert verdict == "auto" and rule == "abbreviation_from_full"


def test_wrong_expansion_stays_for_human():
    # живой мусор из чужого словаря: ADFA расшифрована как Disney Feature Animation
    verdict, _rule, _ = automation.alias_decision("ADFA", "Disney Feature Animation")
    assert verdict == "review"
    assert automation.advice("ADFA", "Disney Feature Animation")["hint"] == "abbreviation_mismatch"


def test_real_synonym_without_derivation_stays_for_human():
    # ЦБ = Банк России — верный синоним, но аббревиатура не выводится:
    # правило обязано молчать, решение принимает человек.
    verdict, _rule, _ = automation.alias_decision("ЦБ", "Банк России")
    assert verdict == "review"


def test_initials_skip_service_words():
    assert automation.initials_match("ФСТЭК", "Федеральная служба по техническому и экспортному контролю")
    assert not automation.initials_match("ФСТЭК", "Федеральная служба безопасности")


def test_answerability_counts_known_words():
    res = answerability.check("какие меры защиты применяет организация")
    assert res["verdict"] in ("covered", "partial")
    assert res["coverage"] > 0
    # служебные слова в счёт не идут
    assert "какие" not in res["covered"] + res["missing"]


def test_answerability_flags_unknown_topic():
    res = answerability.check("сколько потратили на рекламу флешек")
    assert "рекламу" in res["missing"] or "флешек" in res["missing"]
