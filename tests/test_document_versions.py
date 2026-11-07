"""Версии документов (ADR-008): ключ акта, дата, обязательность полей, фильтр поиска.

Тесты структурные и юнит: проверяют контракт, а не живой TypeDB (он проверяется
отдельным скриптом на стенде).
"""
import ast
from pathlib import Path

from src.indexing.typedb_versions import _date_literal, normalize_document_key

ROOT = Path(__file__).resolve().parents[1]
UPLOAD_SRC = (ROOT / "src/api/routes/upload.py").read_text(encoding="utf-8")
SVC_SRC = (ROOT / "src/api/services/document_service.py").read_text(encoding="utf-8")
EMB_SRC = (ROOT / "src/indexing/embeddings_service.py").read_text(encoding="utf-8")
FACTS_SRC = (ROOT / "src/indexing/typedb_facts.py").read_text(encoding="utf-8")


def test_document_key_is_stable():
    """Регистр, «№», кавычки и пробелы не должны давать разные ключи одного акта."""
    a = normalize_document_key("ФСТЭК России", "приказ", "№ 239", "")
    b = normalize_document_key("фстэк  россии", "ПРИКАЗ", "239", "")
    assert a == b
    assert "239" in a
    # Издатель разный — ключ разный: акты разных ведомств не сливаются
    c = normalize_document_key("ФСБ России", "приказ", "239", "")
    assert a != c


def test_document_key_falls_back_to_title():
    """Документ без номера опознаётся по названию, а не по пустой строке."""
    k = normalize_document_key("ФСТЭК России", "методический документ", "", "Методика оценки угроз")
    assert "методика оценки угроз" in k


def test_date_literal_validates_iso():
    assert _date_literal("2021-09-01") == "2021-09-01"
    assert _date_literal("2021-09-01T00:00:00") == "2021-09-01"
    assert _date_literal("01.09.2021") is None
    assert _date_literal("") is None


def test_upload_requires_version_for_regulator():
    tree = ast.parse(UPLOAD_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "upload_document")
    consts = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert "VERSION_REQUIRED" in consts
    seg = ast.get_source_segment(UPLOAD_SRC, fn) or ""
    for field in ('"version_string": version_string', '"effective_from": effective_from',
                  '"supersedes_id": supersedes_id'):
        assert field in seg, field


def test_service_writes_version_after_ontology():
    assert "typedb_versions" in SVC_SRC
    assert "upsert_from_metadata" in SVC_SRC


def test_search_excludes_superseded_by_default():
    assert "include_superseded" in EMB_SRC
    assert 'key="version_status", match=_MAL(value="superseded")' in EMB_SRC


def test_outdated_references_respects_acl():
    assert "def outdated_references" in FACTS_SRC
    assert "def outdated_versions" in FACTS_SRC
    seg = FACTS_SRC.split("def outdated_references")[1]
    assert "chunk not in visible_chunk_ids" in seg
