"""Тесты порядка чанков (страница «Чанки» показывала их вперемешку).

Причина была в ключе сортировки: номер чанка лежит в payload.metadata, а код
читал его с верхнего уровня (там его нет), запасной разбор chunk_id не понимал
формат `<document_id>_chunk_00045` — и у всех чанков ключ оказывался нулём,
то есть сортировка ничего не делала.
"""

from src.api.services.chunk_order import chunk_seq_of, chunk_sort_key


def test_seq_from_metadata():
    payload = {"chunk_id": "d_chunk_00007", "metadata": {"chunk_seq": 7, "chunk_index": 6}}
    assert chunk_seq_of(payload) == 7


def test_seq_from_top_level_when_present():
    assert chunk_seq_of({"chunk_seq": 12}) == 12
    assert chunk_seq_of({"chunk_index": 3}) == 3


def test_metadata_string_seq():
    assert chunk_seq_of({"metadata": {"chunk_seq": "45"}}) == 45


def test_seq_parsed_from_document_prefixed_chunk_id():
    """Новый формат id: <document_id>_chunk_00045 — раньше парсинг падал."""
    payload = {"chunk_id": "5fa8e982-f972-4a82-9935-1976e43709ad_chunk_00045"}
    assert chunk_seq_of(payload) == 45


def test_seq_parsed_from_short_chunk_id():
    assert chunk_seq_of({"chunk_id": "chunk_00003"}) == 3


def test_unknown_seq_is_zero():
    assert chunk_seq_of({"chunk_id": "непонятно"}) == 0
    assert chunk_seq_of({}) == 0
    assert chunk_seq_of(None) == 0


def test_sorting_puts_chunks_in_order():
    payloads = [
        {"document_id": "docA", "chunk_id": "docA_chunk_00003", "metadata": {"chunk_seq": 3}},
        {"document_id": "docA", "chunk_id": "docA_chunk_00001", "metadata": {"chunk_seq": 1}},
        {"document_id": "docA", "chunk_id": "docA_chunk_00002", "metadata": {"chunk_seq": 2}},
    ]
    ordered = [chunk_seq_of(p) for p in sorted(payloads, key=chunk_sort_key)]
    assert ordered == [1, 2, 3]


def test_sorting_groups_by_document():
    payloads = [
        {"document_id": "docB", "metadata": {"chunk_seq": 1}},
        {"document_id": "docA", "metadata": {"chunk_seq": 2}},
        {"document_id": "docA", "metadata": {"chunk_seq": 1}},
    ]
    keys = [chunk_sort_key(p) for p in sorted(payloads, key=chunk_sort_key)]
    assert [k[0] for k in keys] == ["docA", "docA", "docB"]
    assert [k[1] for k in keys] == [1, 2, 1]


def test_chunks_without_seq_go_last_but_stable():
    a = {"document_id": "d", "chunk_id": "d_chunk_00002", "metadata": {"chunk_seq": 2}}
    b = {"document_id": "d", "chunk_id": "d_unknown"}
    c = {"document_id": "d", "chunk_id": "d_zzz"}
    ordered = sorted([b, c, a], key=chunk_sort_key)
    assert ordered[0] is a                      # номер есть — он впереди
    assert [p["chunk_id"] for p in ordered[1:]] == ["d_unknown", "d_zzz"]
