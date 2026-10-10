"""Проверки уточнения типов сущностей правилами.

Смысл: типы, у которых ответ определяется ФОРМОЙ записи, не должны зависеть от модели — замер судьёй
10.10.2026 показал, что модель в них ошибается (пункт «А.1.1» получил тип document_ref, номер ГОСТ —
legal_term). Тесты фиксируют, что правильные формы распознаются, а неопознанные НЕ портятся.
"""

from __future__ import annotations

import pytest

from src.indexing.entity_typing import refine_entities, refine_type


@pytest.mark.parametrize("name", [
    "ГОСТ Р 57580.1-2017", "ГОСТ Р ИСО/МЭК 27001-2021", "ГОСТ 34.601-90",
    "СП 500-131-2007", "СНиП 2.01.02-85", "СанПиН 2.2.2/2.4.1340-03",
])
def test_номера_стандартов_становятся_standard(name):
    assert refine_type(name, "legal_term") == "standard"
    assert refine_type(name, "") == "standard"


@pytest.mark.parametrize("name", ["А.1.1", "5.2.1", "4.4", "п. 4.4", "раздел 6", "таблица 3",
                                  "приложение А", "ст. 5",
                                  # английские формы: в корпусе есть выгрузки SEC
                                  "Section 6.7", "Article 5", "Clause 3.2", "Item 601(b)"])
def test_ссылки_на_пункты_становятся_clause(name):
    assert refine_type(name, "document_ref") == "clause"


@pytest.mark.parametrize("name", ["Exhibit 10.39", "Exhibit 99.1", "Appendix B"])
def test_приложения_становятся_документом(name):
    """«Exhibit 10.39» — это документ (приложение к отчётности), а не пункт."""
    assert refine_type(name, "legal_term") == "document_ref"


@pytest.mark.parametrize("name", ["152-ФЗ", "44-ФЗ", "Приказ ФСТЭК России № 17",
                                  "Указание Банка России от 15.06.2026 № 7369-У",
                                  "Постановление Правительства РФ № 1119"])
def test_реквизиты_документов_становятся_document_ref(name):
    assert refine_type(name, "legal_term") == "document_ref"


@pytest.mark.parametrize("name", ["15.06.2026", "2026-10-10"])
def test_даты_становятся_date(name):
    assert refine_type(name, "legal_term") == "date"


@pytest.mark.parametrize("name,was", [
    ("ключ шифрования", "legal_term"),
    ("Банк России", "organization"),
    ("режим гаммирования", "legal_term"),
    ("средства защиты информации", "legal_term"),
])
def test_неопознанные_формы_не_меняются(name, was):
    """Правила не должны угадывать: где форма не опознана — тип остаётся прежним."""
    assert refine_type(name, was) == was


def test_правки_возвращаются_для_журнала():
    entities = [
        {"name": "ГОСТ Р 34.11", "type": "legal_term"},
        {"name": "А.1.1", "type": "document_ref"},
        {"name": "ключ шифрования", "type": "legal_term"},
    ]
    fixed, changes = refine_entities(entities)
    assert fixed[0]["type"] == "standard"
    assert fixed[1]["type"] == "clause"
    assert fixed[2]["type"] == "legal_term"
    assert len(changes) == 2
    assert ("А.1.1", "document_ref", "clause") in changes


def test_пустой_ввод():
    assert refine_type("", "legal_term") == "legal_term"
    fixed, changes = refine_entities([])
    assert fixed == [] and changes == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
