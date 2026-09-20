"""Проверки «не соврать»: строки-итоги, покрытие числами, единицы измерения, двусмысленность.

Составлено по консультации (reports/_scratch/consult_sql_part3.md): неверная цифра хуже
отсутствия ответа, поэтому перед расчётом смотрим на сами данные, а после — сверяем и
предупреждаем. Отказ («уточните») — нормальный исход, а не сбой.
"""
import importlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database.document_table_models import DocumentTable  # noqa: F401 — нужна в metadata
from src.database.models import Base
from src.database.table_row_models import TableRow  # noqa: F401 — регистрация таблицы
from src.indexing.table_guard import (
    ambiguity_note, detect_units, find_total_rows, numeric_coverage, post_checks, pre_checks,
)
from src.indexing.table_router import answer_from_tables, context_block
from src.indexing.table_store import run_table_query, save_table_rows, table_schema


def _rows(data_rows, headers):
    """Строки в том виде, в каком их отдаёт run_table_query (row_data + row_num)."""
    from src.indexing.table_store import rows_to_records

    out = []
    for rec in rows_to_records(headers, data_rows):
        out.append({"row_index": rec["row_index"], "row_data": rec["row_data"],
                    "row_num": rec["row_num"]})
    return out


HEADERS = ["Наименование", "Количество", "Цена"]


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    mod = importlib.import_module("src.database.session")
    monkeypatch.setattr(mod, "get_session_local", lambda: maker)
    yield maker
    engine.dispose()


# ── строки-итоги ────────────────────────────────────────────────────────────

def test_total_rows_are_found():
    rows = _rows([["Насос", "1", "100"], ["Итого", "", "100"], ["Всего по разделу", "", "100"]],
                 HEADERS)
    assert find_total_rows(rows, "Наименование") == [1, 2]


def test_total_rows_do_not_match_ordinary_names():
    rows = _rows([["Итоговый отчёт", "1", "100"]], HEADERS)
    assert find_total_rows(rows, "Наименование") == [0], "«Итоговый отчёт» — тоже итоговая строка"


def test_sum_excludes_total_rows_and_warns(db):
    save_table_rows("doc-t", "tid-t", HEADERS, [
        ["Насос", "1", "100"],
        ["Втулка", "1", "200"],
        ["Итого", "", "300"],
    ], page_num=1)
    res = answer_from_tables("на какую сумму по цене?")
    assert res["status"] == "ok", res
    assert "300.0" in res["answer"], f"итоговая строка должна быть исключена: {res['answer']}"
    assert "Итого" in (res.get("warning") or ""), res


# ── покрытие числами ────────────────────────────────────────────────────────

def test_numeric_coverage_counts_and_examples():
    rows = _rows([["A", "1", "100"], ["B", "2", "по запросу"], ["C", "3", ""]], HEADERS)
    cov = numeric_coverage(rows, "Цена")
    assert cov["parsed"] == 1 and cov["missing"] == 1
    assert cov["examples"] == ["по запросу"], "пустые ячейки в примеры не берём"


def test_majority_non_numeric_is_a_refusal():
    rows = _rows([["A", "1", "100"], ["B", "2", "по запросу"], ["C", "3", "см. приложение"]],
                 HEADERS)
    checks = pre_checks(rows, "Цена", "Наименование", "sum")
    assert checks["refusals"], "если числа всего одно из трёх — суммировать нельзя"
    assert "уточните" in checks["refusals"][0].lower()


def test_single_missing_value_is_only_a_warning():
    rows = _rows([["A", "1", "100"], ["B", "2", "200"], ["C", "3", "—"]], HEADERS)
    checks = pre_checks(rows, "Цена", "Наименование", "sum")
    assert not checks["refusals"]
    assert any("занижен" in w for w in checks["warnings"])


# ── единицы измерения ───────────────────────────────────────────────────────

def test_mixed_units_are_detected_and_refused():
    rows = _rows([["A", "1", "100 руб."], ["B", "2", "3 тыс. руб."]], HEADERS)
    assert set(detect_units(rows, "Цена")) >= {"руб.", "тыс. руб."}
    checks = pre_checks(rows, "Цена", "Наименование", "sum")
    assert checks["refusals"] and "единиц" in checks["refusals"][0]


def test_same_units_are_fine():
    rows = _rows([["A", "1", "100 руб."], ["B", "2", "200 руб."]], HEADERS)
    checks = pre_checks(rows, "Цена", "Наименование", "sum")
    assert not checks["refusals"]


# ── сверка после расчёта и двусмысленность ──────────────────────────────────

def test_post_check_reports_mismatch_with_declared_total():
    rows = _rows([["A", "1", "100"], ["B", "2", "200"], ["Итого", "", "999"]], HEADERS)
    warnings = post_checks(300.0, rows, "Цена", "Наименование")
    assert any("Итого" in w for w in warnings), "расхождение с контрольной строкой надо сказать"


def test_negative_result_warns():
    rows = _rows([["A", "1", "-100"]], HEADERS)
    warnings = post_checks(-100.0, rows, "Цена", "Наименование")
    assert any("отрицательн" in w for w in warnings)


def test_ambiguity_note_lists_positions():
    rows = _rows([["Насос НЦ-50", "1", "100"], ["Насос НЦ-50 (аналог)", "1", "200"]], HEADERS)
    note = ambiguity_note(rows, "Наименование")
    assert "несколько разных позиций" in note
    assert "Насос НЦ-50" in note


# ── отказ доходит до пользователя ───────────────────────────────────────────

def test_clarification_reaches_context_and_forbids_number(db):
    save_table_rows("doc-u", "tid-u", HEADERS, [
        ["A", "1", "100 руб."],
        ["B", "2", "3 тыс. руб."],
    ], page_num=2)
    res = answer_from_tables("на какую сумму по цене?")
    assert res["status"] == "clarify", res
    block = context_block(res)
    assert "уточняющий вопрос" in block and "Число не называй" in block
    assert "единиц" in block


def test_not_contains_filter_excludes_totals_in_sql(db):
    save_table_rows("doc-v", "tid-v", HEADERS, [
        ["Насос", "1", "100"],
        ["Итого", "", "100"],
    ], page_num=1)
    res = run_table_query({
        "table_id": "tid-v",
        "aggregate": "sum",
        "aggregate_column": "Цена",
        "filters": [{"column": "Наименование", "op": "not_contains", "value": "Итого"}],
    })
    assert res["rows"][0]["value"] == 100.0, res
    assert res["rows"][0]["rows_in_group"] == 1, "итоговая строка не должна попадать в расчёт"


def test_plan_rejects_unknown_op_but_allows_not_contains(db):
    from src.indexing.table_store import validate_plan

    checked = validate_plan({"filters": [{"column": "Цена", "op": "not_contains", "value": "x"}]},
                            HEADERS)
    assert checked["filters"][0]["op"] == "not_contains"
    with pytest.raises(ValueError):
        validate_plan({"filters": [{"column": "Цена", "op": "regexp", "value": "x"}]}, HEADERS)
