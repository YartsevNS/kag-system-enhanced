"""Сторож типизации не должен регистрировать «отказ» LLM как новый тип документа.

Живой случай: сторож поставил документу тип, LLM ответила «unknown» — и этот ответ
попал в список типов (в логе «Новый тип: unknown»). На стенде так и было: в списке
лежали unknown и тестовыйтип.
"""
import pytest

from src.indexing.type_watchdog import is_registrable_doc_type


@pytest.mark.parametrize("dtype", ["unknown", "Unknown", "OTHER", "other", "неизвестно", "-", "n/a", "", None, 42])
def test_refusals_are_not_registered(dtype):
    assert is_registrable_doc_type(dtype) is False, f"{dtype!r} — не тип документа"


@pytest.mark.parametrize("dtype", ["приказ", "ГОСТ Р ИСО/МЭК 15408-2-2013", "инструкция", "стандарт"])
def test_real_types_are_registered(dtype):
    assert is_registrable_doc_type(dtype) is True


def test_too_long_answer_is_not_registered():
    assert is_registrable_doc_type("о" * 40) is False, "длинная фраза — не название типа"
