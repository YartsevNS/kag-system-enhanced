"""Этап 1 табличного стека: разметка таблиц, качество, атомарные чанки.

Что проверяем и зачем:
- `table_id` устойчив и детерминирован (иначе строки таблицы после переиндексации
  оказываются под разными id, и поиск по строкам разваливается);
- качество извлечения считается и отличает таблицу от плоского текста;
- табличный сегмент становится ОТДЕЛЬНЫМ чанком и не склеивается с текстом страницы
  (иначе в одном векторе оказываются строки разных таблиц и текст — именно на этом
  ломались вопросы вида «какая цена у X»);
- табличные поля доходят до payload Qdrant;
- настройки табличного стека читаются с дефолтами и не ломают обработку при мусоре.
"""
import importlib
import re
from pathlib import Path

import pytest

from src.indexing.table_ids import (
    make_table_id, table_quality, table_stats, tables_are_separate,
)
from src.indexing.tables_settings import DEFAULTS, get_tables_config, tables_enabled
from src.indexing.chunking import DocumentChunker

ROOT = Path(__file__).resolve().parents[1]
TABLE_SEG = {
    "type": "table",
    "content": "| Наименование | Количество | Цена |\n|---|---|---|\n| Насос НЦ-50 | 12 | 15400,50 |",
    "page": 1,
    "metadata": {
        "chunk_type": "table",
        "is_table": True,
        "table_id": "tid-1",
        "table_index": 0,
        "row_count": 2,
        "col_count": 3,
        "quality": 0.9,
        "columns": ["Наименование", "Количество", "Цена"],
    },
}
TEXT_SEG_1 = {"type": "text", "content": "Текст первой страницы. " * 4, "page": 1, "metadata": {}}
TEXT_SEG_2 = {"type": "text", "content": "Текст второй страницы.", "page": 2, "metadata": {}}


# ── table_id ────────────────────────────────────────────────────────────────

def test_table_id_is_deterministic():
    a = make_table_id("doc-1", 3, 0)
    b = make_table_id("doc-1", 3, 0)
    assert a == b, "переиндексация того же документа должна давать тот же table_id"
    assert re.fullmatch(r"[0-9a-f-]{36}", a), f"ожидали uuid, получили {a!r}"


def test_table_id_differs_by_document_page_and_index():
    base = make_table_id("doc-1", 1, 0)
    assert base != make_table_id("doc-2", 1, 0), "разные документы — разные таблицы"
    assert base != make_table_id("doc-1", 2, 0), "та же таблица на другой странице — другой id"
    assert base != make_table_id("doc-1", 1, 1), "вторая таблица на той же странице — другой id"


# ── качество и размеры таблицы ──────────────────────────────────────────────

def test_quality_of_good_table_is_high():
    rows = [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]]
    assert table_quality(rows) == 1.0


@pytest.mark.parametrize("rows", [
    [["A", "B", "C"]],                       # одна строка — не таблица
    [["A"], ["1"], ["2"]],                   # одна колонка
    [["", ""], ["", ""]],                    # пусто
    [],
    None,
])
def test_quality_of_non_table_is_zero(rows):
    assert table_quality(rows) == 0.0


def test_quality_penalizes_ragged_rows():
    ok = table_quality([["A", "B"], ["1", "2"], ["3", "4"]])
    ragged = table_quality([["A", "B"], ["1"], ["2", "3"]])
    assert 0 < ragged < ok, f"рваные строки должны быть хуже: {ragged} против {ok}"


def test_stats_count_rows_columns_and_numbers():
    stats = table_stats([["Наименование", "Количество"], ["Насос", "12"], ["Втулка", "1 234,56"]])
    assert stats["row_count"] == 3
    assert stats["col_count"] == 2
    assert stats["width_consistent"] is True
    assert stats["has_numeric"] is True, "числа с пробелом-разделителем и запятой — числа"


def test_tables_are_separate_counts_table_segments():
    assert tables_are_separate([TEXT_SEG_1, TABLE_SEG, TEXT_SEG_2]) == 1
    assert tables_are_separate([TEXT_SEG_1, TEXT_SEG_2]) == 0


# ── настройки табличного стека ──────────────────────────────────────────────

def test_config_defaults_when_settings_empty():
    cfg = get_tables_config()
    for key in ("enabled", "atomic_chunks", "row_vectors", "sql_enabled"):
        assert key in cfg, f"в конфиге нет {key}"
    assert tables_enabled() is True


def test_config_merges_known_keys_and_ignores_junk(monkeypatch):
    mod = importlib.import_module("src.api.services.config_store")

    class _FakeStore:
        def get(self, category, key="default", default=None):
            return {"row_vectors": False, "unknown_key": 42, "enabled": None}

    monkeypatch.setattr(mod, "config_store", _FakeStore())
    cfg = get_tables_config()
    assert cfg["row_vectors"] is False, "известный ключ должен примениться"
    assert "unknown_key" not in cfg, "неизвестные ключи не тащим дальше"
    assert cfg["enabled"] == DEFAULTS["enabled"], "None не должен затирать дефолт"


def test_config_survives_broken_store(monkeypatch):
    mod = importlib.import_module("src.api.services.config_store")

    class _Broken:
        def get(self, *a, **kw):
            raise RuntimeError("БД недоступна")

    monkeypatch.setattr(mod, "config_store", _Broken())
    cfg = get_tables_config()
    assert cfg["enabled"] is True, "сбой чтения настроек не должен ломать обработку"


# ── чанкинг: таблица отдельным чанком ───────────────────────────────────────

def _chunks(segments, **kw):
    return DocumentChunker(**kw).chunk_segments(segments, "doc-1")


def test_table_becomes_its_own_chunk():
    chunks = _chunks([TEXT_SEG_1, TABLE_SEG, TEXT_SEG_2])
    assert len(chunks) == 3, f"ждали три отдельных чанка, получили {len(chunks)}"

    tables = [c for c in chunks if c["metadata"].get("chunk_type") == "table"]
    assert len(tables) == 1, "табличный чанк должен быть один"
    table = tables[0]
    assert table["content"] == TABLE_SEG["content"], "таблица не должна обрастать текстом страницы"
    assert table["metadata"]["table_id"] == "tid-1"
    assert table["metadata"]["row_count"] == 2
    assert table["metadata"]["columns"] == ["Наименование", "Количество", "Цена"]
    assert table["metadata"]["overlap_applied"] is False, "хвост текста в таблицу не подставляем"

    for text_chunk in [c for c in chunks if c is not table]:
        assert text_chunk["metadata"]["chunk_type"] == "text"
        assert "table_id" not in text_chunk["metadata"], "табличные поля не должны протекать в текст"


def test_tables_are_not_merged_with_each_other():
    second = dict(TABLE_SEG)
    second["metadata"] = {**TABLE_SEG["metadata"], "table_id": "tid-2", "table_index": 1}
    chunks = _chunks([TABLE_SEG, second])
    assert len(chunks) == 2, "две таблицы подряд — два чанка, а не один общий"
    ids = [c["metadata"].get("table_id") for c in chunks]
    assert ids == ["tid-1", "tid-2"]


def test_giant_table_is_split_but_keeps_table_id():
    big = dict(TABLE_SEG)
    big["content"] = "| колонка A | колонка B |\n|---|---|\n" + "\n".join(
        f"| значение {i} | {i * 10} |" for i in range(60)
    )
    # overlap=0 здесь важен: проверяем, что части таблицы не подмешивают текст соседей
    chunks = _chunks([big], chunk_size=200, chunk_overlap=0)
    assert len(chunks) > 1, "таблица длиннее лимита должна резаться"
    assert all(c["metadata"]["table_id"] == "tid-1" for c in chunks), \
        "у всех частей одной таблицы должен остаться тот же table_id"
    assert all(c["metadata"].get("is_partial") is True for c in chunks)


def test_atomic_chunks_can_be_switched_off(monkeypatch):
    """Настройка atomic_chunks выключает новое поведение (возврат к прежнему чанкингу)."""
    mod = importlib.import_module("src.indexing.tables_settings")
    monkeypatch.setattr(mod, "get_tables_config", lambda: {"atomic_chunks": False})
    chunks = _chunks([TEXT_SEG_1, TABLE_SEG, TEXT_SEG_2])
    assert len(chunks) == 1, "при выключенной настройке таблица снова склеивается с текстом"


# ── контракты: разметка доходит до payload и до Postgres ────────────────────

def test_embeddings_payload_promotes_table_fields():
    src = (ROOT / "src/indexing/embeddings_service.py").read_text(encoding="utf-8")
    block = src[src.index("structure = {"): src.index("payload = {")]
    for key in ("chunk_type", "table_id", "table_index", "row_count", "columns"):
        assert f'"{key}"' in block, f"поле {key} не поднимается в payload Qdrant"


def test_document_service_writes_table_id_and_quality():
    src = (ROOT / "src/api/services/document_service.py").read_text(encoding="utf-8")
    assert "make_table_id(document_id" in src, "table_id должен считаться от документа и места"
    assert "table_id=_tid" in src, "table_id не записывается в document_tables"
    assert "quality=float(tb.get('quality')" in src, "качество извлечения не сохраняется"
    assert "row_count=len(data_rows)" in src, "число строк не сохраняется"
    # Нумерация таблиц сквозная по документу — иначе id таблиц на разных страницах совпадут
    assert src.count("_table_seq = 0") == 2, "и в сегментах, и в сохранении нужен сквозной счётчик"


def test_migrations_add_table_columns():
    src = (ROOT / "src/database/migrations.py").read_text(encoding="utf-8")
    for col in ("table_id", "row_count", "quality"):
        assert f'("document_tables", "{col}"' in src, f"нет миграции колонки {col}"
    assert "ix_dt_table_id" in src, "индекс по table_id нужен: поиск строк идёт по нему"
