"""Этап 2 табличного стека: строчный слой (строки таблиц) и безопасные запросы.

Что проверяем и зачем:
- строки таблицы сохраняются отдельными записями и перезаписываются идемпотентно
  (переиндексация не должна удваивать строки);
- оригинал значения и распознанное число живут рядом («15400,50» показываем, 15400.5 считаем);
- поиск и вычисление разделены: суммы и фильтры считает SQL, а не модель по тексту;
- план запроса проверяется: колонка обязана быть в таблице, операция — из белого списка,
  значение уходит параметром. Иначе модель могла бы сломать запрос или вытащить лишнее.
"""
import importlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database.models import Base
from src.database.document_table_models import DocumentTable  # noqa: F401 — нужна в metadata для create_all
from src.database.table_row_models import TableRow  # noqa: F401 — регистрация таблицы
from src.indexing.table_store import (
    build_row_text, normalize_headers, parse_number, rows_to_records,
    rows_for_table, run_table_query, save_table_rows, table_schema, validate_plan,
)

HEADERS = ["Наименование", "Количество", "Цена"]
ROWS = [
    ["Насос НЦ-50", "12", "15400,50"],
    ["Насос НЦ-50", "3", "16000"],
    ["Втулка В-2", "100", "120,25"],
    ["Прокладка", "—", ""],
]


@pytest.fixture
def db(monkeypatch):
    """In-memory SQLite + подмена session factory: тесты не ходят в настоящую БД."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    mod = importlib.import_module("src.database.session")
    monkeypatch.setattr(mod, "get_session_local", lambda: maker)
    yield maker
    engine.dispose()


# ── разбор значений и сборка строк ──────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("15400,50", 15400.50),
    ("1 234,56", 1234.56),
    ("1.234,56", 1234.56),
    ("1234.56", 1234.56),
    ("12%", 12.0),
    ("-15", -15.0),
    ("—", None),
    ("нет данных", None),
    ("", None),
    (None, None),
    ("12 шт", 12.0),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_row_text_is_key_value_pairs():
    text = build_row_text(HEADERS, ROWS[0])
    assert text == "Наименование: Насос НЦ-50; Количество: 12; Цена: 15400,50"


def test_empty_header_gets_placeholder_name():
    keys = normalize_headers(["Наименование", "", None])
    assert keys == ["Наименование", "колонка_1", "колонка_2"], \
        "колонка без имени не должна теряться — иначе по ней нельзя считать"


def test_records_keep_original_and_number():
    recs = rows_to_records(HEADERS, ROWS)
    assert recs[0]["row_data"]["Цена"] == "15400,50", "оригинал сохраняем как в документе"
    assert recs[0]["row_num"]["Цена"] == 15400.5, "рядом храним распознанное число"
    assert recs[3]["row_num"] == {}, "«—» и пусто — не числа"


# ── сохранение и чтение ─────────────────────────────────────────────────────

def test_save_and_read_rows(db):
    saved = save_table_rows("doc-1", "tid-1", HEADERS, ROWS, page_num=7)
    assert saved == 4

    rows = rows_for_table("tid-1")
    assert len(rows) == 4
    assert rows[0]["row_index"] == 0 and rows[3]["row_index"] == 3
    assert rows[0]["row_data"]["Наименование"] == "Насос НЦ-50"
    assert rows[0]["page_num"] == 7


def test_resave_replaces_not_duplicates(db):
    save_table_rows("doc-1", "tid-1", HEADERS, ROWS)
    save_table_rows("doc-1", "tid-1", HEADERS, ROWS[:2])
    rows = rows_for_table("tid-1")
    assert len(rows) == 2, "повторное сохранение той же таблицы должно перезаписывать строки"


def test_schema_lists_tables_and_columns(db):
    save_table_rows("doc-1", "tid-1", HEADERS, ROWS, page_num=1)
    save_table_rows("doc-1", "tid-2", ["Код", "Значение"], [["A", "1"]], page_num=3)
    schema = {t["table_id"]: t for t in table_schema("doc-1")}
    assert set(schema) == {"tid-1", "tid-2"}
    assert schema["tid-1"]["columns"] == HEADERS
    assert schema["tid-1"]["row_count"] == 4
    assert schema["tid-2"]["page_num"] == 3


# ── проверка плана ──────────────────────────────────────────────────────────

def test_plan_rejects_unknown_column():
    with pytest.raises(ValueError, match="нет в таблице"):
        validate_plan({"filters": [{"column": "СекретнаяКолонка", "op": "=", "value": "x"}]}, HEADERS)


def test_plan_rejects_unknown_operation():
    with pytest.raises(ValueError, match="не разрешена"):
        validate_plan({"filters": [{"column": "Цена", "op": "DROP", "value": "1"}]}, HEADERS)


def test_plan_rejects_unknown_aggregate():
    with pytest.raises(ValueError, match="не разрешён"):
        validate_plan({"aggregate": "exec", "aggregate_column": "Цена"}, HEADERS)


def test_plan_accepts_valid_and_drops_extra():
    checked = validate_plan({
        "table_id": "tid-1",
        "filters": [{"column": "Количество", "op": ">", "value": "10"}],
        "aggregate": "sum",
        "aggregate_column": "Цена",
        "sql": "DROP TABLE documents",   # лишние поля не проходят дальше
    }, HEADERS)
    assert checked["aggregate"] == "sum"
    assert "sql" not in checked, "произвольный SQL от модели в план не пропускаем"


# ── вычисления и фильтры на реальном SQL ────────────────────────────────────

def test_sum_with_text_filter(db):
    save_table_rows("doc-1", "tid-1", HEADERS, ROWS)
    res = run_table_query({
        "table_id": "tid-1",
        "filters": [{"column": "Наименование", "op": "contains", "value": "НЦ-50"}],
        "aggregate": "sum",
        "aggregate_column": "Цена",
    })
    assert "error" not in res, res
    assert res["rows"][0]["value"] == pytest.approx(15400.50 + 16000.0)
    assert res["rows"][0]["rows_in_group"] == 2, "считаем только строки, попавшие в фильтр"


def test_numeric_filter_and_count(db):
    save_table_rows("doc-1", "tid-1", HEADERS, ROWS)
    res = run_table_query({
        "table_id": "tid-1",
        "filters": [{"column": "Количество", "op": ">", "value": "10"}],
        "aggregate": "count",
    })
    assert res["rows"][0]["value"] == 2, "12 и 100 больше 10; «—» не число и не считается"


def test_group_by_returns_groups(db):
    save_table_rows("doc-1", "tid-1", HEADERS, ROWS)
    res = run_table_query({
        "table_id": "tid-1",
        "aggregate": "sum",
        "aggregate_column": "Цена",
        "group_by": "Наименование",
    })
    groups = {r["group_key"]: r for r in res["rows"]}
    assert set(groups) == {"Насос НЦ-50", "Втулка В-2", "Прокладка"}
    assert groups["Насос НЦ-50"]["value"] == pytest.approx(31400.50)


def test_limit_marks_truncated(db):
    save_table_rows("doc-1", "tid-1", HEADERS, ROWS)
    res = run_table_query({"table_id": "tid-1"}, max_rows=2)
    assert res["row_count"] == 2
    assert res["truncated"] is True


def test_query_of_unknown_table_returns_error_not_exception(db):
    res = run_table_query({"table_id": "нет-такой", "aggregate": "count"})
    assert "error" in res and res["row_count"] == 0


def test_string_comparison_with_unknown_value_is_rejected(db):
    """«Цена > дорого» — не число: сравнение отклоняем, а не угадываем смысл."""
    save_table_rows("doc-1", "tid-1", HEADERS, ROWS)
    res = run_table_query({
        "table_id": "tid-1",
        "filters": [{"column": "Цена", "op": ">", "value": "дорого"}],
    })
    assert "error" in res


# ── заполнение строчного слоя по уже сохранённым таблицам ───────────────────

def _load_backfill_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts/backfill_table_rows.py"
    spec = importlib.util.spec_from_file_location("backfill_table_rows", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_table(session, document_id="doc-old", table_id="", quality=0.0):
    """Запись document_tables в том виде, в каком её оставила прежняя версия кода."""
    import json

    from src.database.document_table_models import DocumentTable

    session.add(DocumentTable(
        id="row-1",
        document_id=document_id,
        page_num=4,
        table_index=0,
        table_id=table_id,
        row_count=0,
        quality=quality,
        rows_json=json.dumps([["Насос НЦ-50", "12", "15400,50"], ["Втулка", "3", "120,25"]],
                             ensure_ascii=False),
        headers_json=json.dumps(HEADERS, ensure_ascii=False),
        markdown="| ... |",
        html="<table></table>",
        model="pymupdf",
    ))
    session.commit()


def test_backfill_fills_rows_ids_and_quality(db):
    module = _load_backfill_module()
    session = db()
    _legacy_table(session)
    session.close()
    # ВАЖНО: get_session_local возвращает ФАБРИКУ сессий, а не сессию — подменяем именно её
    module.get_session_local = lambda: db

    stats = module.backfill()
    assert stats["tables"] == 1 and stats["rows"] == 2
    assert stats["fixed_ids"] == 1 and stats["fixed_quality"] == 1

    from src.database.document_table_models import DocumentTable

    session = db()
    table = session.query(DocumentTable).filter_by(id="row-1").one()
    assert table.table_id, "без table_id строки нельзя связать с таблицей"
    assert table.row_count == 2
    assert table.quality > 0

    stored = rows_for_table(table.table_id)
    assert len(stored) == 2
    assert stored[0]["row_data"]["Наименование"] == "Насос НЦ-50"
    assert stored[0]["row_num"]["Цена"] == 15400.50
    session.close()


def test_backfill_dry_run_writes_nothing(db):
    module = _load_backfill_module()
    session = db()
    _legacy_table(session, document_id="doc-dry")
    session.close()
    module.get_session_local = lambda: db

    stats = module.backfill(dry_run=True)
    assert stats["tables"] == 1 and stats["rows"] == 2
    assert table_schema("doc-dry") == [], "пробный прогон не должен ничего записывать"
