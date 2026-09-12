"""Тесты извлечения структурных метаданных (standard_parser)."""

import pytest

from src.indexing.standard_parser import (
    extract_clause,
    extract_section,
    extract_standard_number,
    extract_structure,
)


@pytest.mark.parametrize("text,expected", [
    ("Требования ГОСТ Р 56545-2015 к защите", "ГОСТ Р 56545-2015"),
    ("в соответствии с ГОСТ 57580.1-2017", "ГОСТ 57580.1-2017"),
    ("ГОСТ Р ИСО/МЭК 27001-2021", "ГОСТ Р ИСО/МЭК 27001-2021"),
    ("по СТО БР БФБО-1.9-2024 организация обязана", "СТО БР БФБО-1.9-2024"),
    ("ISO/IEC 27001:2022 устанавливает", "ISO/IEC 27001:2022"),
    ("Приказ ФСТЭК России № 17 определяет", "Приказ ФСТЭК России № 17"),
])
def test_standard_number_found(text, expected):
    assert extract_standard_number(text) == expected


@pytest.mark.parametrize("text", [
    "обычный текст без ссылок на стандарты",
    "цена 1500 рублей за единицу",
    "",
])
def test_standard_number_absent(text):
    assert extract_standard_number(text) is None


@pytest.mark.parametrize("text,expected", [
    ("согласно п. 5.2.1 настоящего документа", "5.2.1"),
    ("пункт 5.2.1.3 требует", "5.2.1.3"),
    ("п.п. 4.1 и 4.2", "4.1"),
    ("5.2.1 Требования к системе", "5.2.1"),
    ("10.3 Оценка соответствия", "10.3"),
])
def test_clause_found(text, expected):
    assert extract_clause(text) == expected


@pytest.mark.parametrize("text", [
    "в 2024 году было 17 инцидентов",
    "текст без нумерации",
    "",
])
def test_clause_absent(text):
    assert extract_clause(text) is None


def test_section():
    assert extract_section("см. раздел 5 настоящего документа") == "5"
    assert extract_section("глава 3") == "3"
    assert extract_section("нет ссылок") is None


def test_extract_structure_combines():
    text = "ГОСТ Р 56545-2015, п. 5.2.1: требования к защите информации"
    got = extract_structure(text)
    assert got["standard_number"] == "ГОСТ Р 56545-2015"
    assert got["clause"] == "5.2.1"


def test_extract_structure_empty_when_nothing():
    assert extract_structure("просто текст") == {}


def test_deterministic_and_idempotent():
    text = "ГОСТ 57580.1-2017 п. 7.1.2"
    assert extract_standard_number(text) == extract_standard_number(text)
    assert extract_clause(text) == extract_clause(text)
