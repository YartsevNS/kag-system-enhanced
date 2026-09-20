"""Этап 4 табличного стека: вопрос → план → SQL-ответ.

Что проверяем и зачем:
- канонические роли колонок: «на какую сумму» должно находить «Цена, руб.» — иначе SQL
  просто не построится на реальных документах, где колонки названы как угодно;
- арифметика решается КОДОМ: сумма идёт по колонке суммы, количество — по количеству;
- четыре разных исхода не смешиваются: посчитано / ноль по условию / нет данных /
  не понял вопрос. Пользователь должен видеть, какой это случай;
- если часть значений не числа, сумма занижена — об этом говорим прямо, а не молчим.

Тесты идут на настоящем SQL (SQLite in-memory) через table_store, поэтому проверяют
весь путь: сопоставление колонок → план → запрос → текст ответа.
"""
import importlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database.document_table_models import DocumentTable  # noqa: F401 — нужна в metadata
from src.database.models import Base
from src.database.table_row_models import TableRow  # noqa: F401 — регистрация таблицы
from src.indexing.table_router import (
    answer_from_tables, build_plan, context_block, detect_aggregate, detect_filters,
    map_columns, pick_table,
)
from src.indexing.table_store import save_table_rows

HEADERS = ["Наименование", "Артикул", "Количество, шт", "Цена, руб.", "Сумма, руб."]
ROWS = [
    ["Насос НЦ-50", "АБВ-123", "12", "15400,50", "184806"],
    ["Насос НЦ-50", "АБВ-124", "3", "16000", "48000"],
    ["Втулка В-2", "ГДЕ-777", "100", "120,25", "12025"],
    ["Прокладка", "ЖЗИ-001", "—", "по запросу", "—"],
]


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


@pytest.fixture
def tables(db):
    save_table_rows("doc-spec", "tid-spec", HEADERS, ROWS, page_num=7)
    return db


# ── роли колонок ────────────────────────────────────────────────────────────

def test_roles_from_real_headers():
    roles = map_columns(HEADERS)
    assert roles["name"] == "Наименование"
    assert roles["article"] == "Артикул"
    assert roles["quantity"] == "Количество, шт", "единицы измерения в заголовке не должны мешать"
    assert roles["price"] == "Цена, руб."
    assert roles["amount"] == "Сумма, руб."


def test_role_matched_from_question_words():
    roles = map_columns(["Стоимость всего", "Наименование объекта"], "какая стоимость всего?")
    assert roles["amount"] == "Стоимость всего"
    assert roles["name"] == "Наименование объекта"


def test_role_not_duplicated():
    roles = map_columns(["Цена", "Цена за единицу"])
    assert list(roles.values()).count(roles["price"]) == 1, "одна роль — одна колонка"


# ── понимание вопроса ───────────────────────────────────────────────────────

@pytest.mark.parametrize("question,expected", [
    ("Сколько всего позиций насосов НЦ-50 и на какую сумму?", "sum"),
    ("На какую сумму закупка?", "sum"),
    ("Какая максимальная цена в спецификации?", "max"),
    ("Какая самая дешёвая позиция?", "min"),
    ("Средняя цена по позициям", "avg"),
    ("Сколько позиций в таблице?", "count"),
    ("Дай список позиций", None),
])
def test_detect_aggregate(question, expected):
    assert detect_aggregate(question, map_columns(HEADERS)) == expected


def test_detect_numeric_filter_with_role():
    filters = detect_filters("Какие позиции дороже 10 000 рублей?", map_columns(HEADERS))
    assert filters and filters[0]["op"] == ">"
    assert filters[0]["column"] == "Цена, руб."
    assert filters[0]["value"] == 10000


def test_detect_name_filter():
    filters = detect_filters("Есть ли в спецификации позиция с артикулом АБВ-123?", map_columns(HEADERS))
    columns = {f["column"] for f in filters}
    assert "Наименование" in columns, "название позиции должно уходить в фильтр по наименованию"
    assert all(f["op"] == "contains" for f in filters)


# ── выбор таблицы и план ────────────────────────────────────────────────────

def test_pick_table_prefers_search_documents():
    entries = [
        {"table_id": "t1", "document_id": "doc-a", "page_num": 1,
         "columns": ["Наименование", "Цена"], "row_count": 5},
        {"table_id": "t2", "document_id": "doc-b", "page_num": 2,
         "columns": ["Наименование", "Количество", "Цена"], "row_count": 9},
    ]
    first = pick_table("какая цена у позиции?", entries)
    preferred = pick_table("какая цена у позиции?", entries, prefer_documents=["doc-a"])
    assert first.table_id == "t2", "по умолчанию выигрывает таблица с большим покрытием ролей"
    assert preferred.table_id == "t1", "документ из результатов поиска — сильный признак"


def test_plan_uses_amount_column_for_sum():
    entries = [{"table_id": "t1", "document_id": "doc-a", "page_num": 7,
                "columns": HEADERS, "row_count": 4}]
    candidate = pick_table("на какую сумму?", entries)
    plan = build_plan("на какую сумму?", candidate)
    assert plan["aggregate"] == "sum"
    assert plan["aggregate_column"] == "Сумма, руб.", "сумма берётся из колонки суммы"


def test_plan_reports_missing_column_instead_of_guessing():
    entries = [{"table_id": "t1", "document_id": "doc-a", "page_num": 1,
                "columns": ["Наименование", "Комментарий"], "row_count": 3}]
    candidate = pick_table("на какую сумму?", entries)
    plan = build_plan("на какую сумму?", candidate)
    assert "error" in plan, "если колонки для суммы нет — говорим об этом, а не считаем наугад"


# ── ответы целиком ──────────────────────────────────────────────────────────

def test_sum_answer_with_filter(tables):
    res = answer_from_tables("Сколько всего позиций насосов НЦ-50 и на какую сумму?")
    assert res["status"] == "ok", res
    assert "184806" in res["answer"] or "232806" in res["answer"], res["answer"]
    assert "стр. 7" in res["source"]
    assert res["row_count"] >= 1


def test_count_answer(tables):
    res = answer_from_tables("Сколько позиций в спецификации?")
    assert res["status"] == "ok"
    assert "4" in res["answer"], res["answer"]


def test_filter_answer_returns_rows(tables):
    res = answer_from_tables("Какие позиции дороже 10 000 рублей?")
    assert res["status"] == "ok", res
    assert "Насос НЦ-50" in res["answer"]


def test_zero_rows_is_not_an_error(tables):
    res = answer_from_tables("Есть ли позиция ААА-999 в спецификации?")
    assert res["status"] == "no_rows", res
    assert "ничего не найдено" in res["message"]


def test_no_parsable_column_gives_no_data(db):
    save_table_rows("doc-x", "tid-x", ["Наименование", "Примечание"],
                    [["Позиция", "см. приложение"]], page_num=1)
    res = answer_from_tables("на какую сумму?")
    assert res["status"] == "no_data", res
    assert "нет колонки" in res["message"] or "нет колонки" in str(res.get("reason", ""))


def test_unparsed_question_without_tables(db):
    res = answer_from_tables("расскажи про порядок разработки")
    assert res["status"] in ("no_data", "unparsed")
    assert res.get("reason") in ("не понял вопрос", "нет данных")


def test_warning_when_values_are_not_numbers(db):
    """Часть значений не числа — сумма занижена, и об этом надо сказать."""
    save_table_rows("doc-y", "tid-y", ["Наименование", "Цена"], [
        ["Позиция 1", "100"],
        ["Позиция 2", "по запросу"],
    ], page_num=1)
    res = answer_from_tables("на какую сумму по цене?")
    assert res["status"] == "ok", res
    assert "занижен" in (res.get("warning") or ""), res


def test_context_block_states_that_sql_was_used(tables):
    res = answer_from_tables("Сколько позиций в спецификации?")
    block = context_block(res)
    assert "посчитаны SQL" in block
    assert "Не пересчитывай сам" in block


def test_context_block_for_empty_result(tables):
    res = answer_from_tables("Есть ли позиция ААА-999 в спецификации?")
    block = context_block(res)
    assert "ничего не найдено" in block


# ── контракт с чатом ────────────────────────────────────────────────────────

def test_chat_calls_tables_layer_without_blocking_api():
    """Чат обязан звать табличный слой в отдельном потоке и только по настройке."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/api/services/chat_service.py").read_text(
        encoding="utf-8")
    assert "answer_from_tables" in src, "чат не использует вычисления по таблицам"
    assert "context_block" in src, "результат вычислений не попадает в контекст"
    assert 'get_tables_config().get("sql_enabled"' in src, "нет выключателя табличных вычислений"
    block = src[src.index("2c. Вычисления по таблицам"):]
    assert "asyncio.to_thread" in block[:1200], \
        "запросы к БД синхронные: без to_thread api заблокируется"


def test_sql_settings_defaults_are_on():
    from src.indexing.tables_settings import DEFAULTS

    assert DEFAULTS["sql_enabled"] is True
    assert DEFAULTS["sql_max_rows"] > 0 and DEFAULTS["sql_timeout_ms"] > 0
