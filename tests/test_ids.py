"""Тесты единого идентификатора и текста для эмбеддинга."""

import pytest

from src.indexing.ids import build_embedding_text, point_id_for_chunk


def test_point_id_deterministic():
    cid = "doc123_chunk_00001"
    assert point_id_for_chunk(cid) == point_id_for_chunk(cid)


def test_point_id_differs_by_chunk():
    a = point_id_for_chunk("doc123_chunk_00001")
    b = point_id_for_chunk("doc123_chunk_00002")
    assert a != b


def test_point_id_is_uuid():
    import uuid
    value = point_id_for_chunk("doc_chunk_1")
    parsed = uuid.UUID(value)
    assert parsed.version == 5


def test_point_id_namespace_isolated():
    """uuid5 от «kag-chunk:x» не должен совпасть с uuid5 от «x»."""
    import uuid
    assert point_id_for_chunk("x") != str(uuid.uuid5(uuid.NAMESPACE_DNS, "x"))


def test_point_id_requires_chunk_id():
    with pytest.raises(ValueError):
        point_id_for_chunk("")


def test_point_id_no_collision_between_documents():
    """Одинаковый chunk_id у разных документов НЕ должен давать один point_id.

    Регрессия 2026-09-12: chunk_id вида «chunk_00001» (без префикса документа)
    встречается во всех документах — без document_id в ключе точки Qdrant
    перезаписывали друг друга (документы терялись).
    """
    a = point_id_for_chunk("chunk_00001", "doc-A")
    b = point_id_for_chunk("chunk_00001", "doc-B")
    assert a != b


def test_point_id_document_id_included():
    """С document_id и без него — разные id (обратная совместимость не ломается)."""
    assert point_id_for_chunk("chunk_1", "doc-1") != point_id_for_chunk("chunk_1")


@pytest.mark.parametrize("meta,prefix", [
    ({"standard_number": "ГОСТ Р 56545-2015"}, "ГОСТ Р 56545-2015: "),
    ({"clause": "5.2.1"}, "п. 5.2.1: "),
    ({"standard_number": "ГОСТ 57580.1-2017", "clause": "5.2.1"}, "ГОСТ 57580.1-2017, п. 5.2.1: "),
    ({"section": "7"}, "раздел 7: "),
])
def test_embedding_text_prefix(meta, prefix):
    text = build_embedding_text("Требования к защите", meta)
    assert text.startswith(prefix)
    assert text.endswith("Требования к защите")


def test_embedding_text_without_structure_unchanged():
    assert build_embedding_text("обычный текст", {}) == "обычный текст"
    assert build_embedding_text("обычный текст", None) == "обычный текст"


def test_embedding_text_clause_wins_over_section():
    text = build_embedding_text("текст", {"clause": "5.2.1", "section": "5"})
    assert text.startswith("п. 5.2.1: ")
