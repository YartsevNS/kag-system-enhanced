"""Тесты адресного включения гибридного поиска (lexical_only).

Замер 2026-09-12: на семантических вопросах лексическая ветка и любой фьюжн
(RRF/DBSF) опускают MRR ниже плотного поиска (0.774/0.767 против 0.821).
Поэтому гибрид включается адресно — для запросов с точными терминами, где
BM25 объективно силён.
"""

from src.indexing.embeddings_service import EmbeddingsService as ES


def test_standard_numbers_trigger_lexical():
    for q in ("меры защиты по ГОСТ 50922",
              "требования ГОСТ Р 56545-2015",
              "что в ISO/IEC 27001:2022",
              "СТО БР БФБО-1.9-2024"):
        assert ES._query_prefers_lexical(q), q


def test_document_references_trigger_lexical():
    for q in ("указание Банка России № 7403-У",
              "приказ ФСТЭК 17 требования",
              "положение 590-П о резервах",
              "пункт 5.2.1 стандарта"):
        assert ES._query_prefers_lexical(q), q


def test_plain_semantic_queries_do_not_trigger():
    """Обычные формулировки идут плотным поиском — там он сильнее."""
    for q in ("текущая ситуация в российской экономике",
              "инфляция и динамика потребительских цен",
              "внешнеторговые бартерные сделки",
              "цифровая финансовая система России",
              "требования к защите информации в финансовых организациях"):
        assert not ES._query_prefers_lexical(q), q


def test_empty_query_is_not_lexical():
    assert not ES._query_prefers_lexical("")
    assert not ES._query_prefers_lexical(None)


def test_quotes_trigger_lexical():
    assert ES._query_prefers_lexical('найти "бартерные сделки"')
    assert ES._query_prefers_lexical("что такое «ключевая ставка»")


# ── режимы поиска для админки ──────────────────────────────────────────────

def test_search_modes_declared():
    assert ES.SEARCH_MODES == ("dense", "hybrid_auto", "hybrid_always")


def test_mode_mapping_to_config():
    assert ES.config_for_search_mode("dense") == {"sparse_enabled": False}
    assert ES.config_for_search_mode("hybrid_auto") == {
        "sparse_enabled": True, "sparse_mode": "lexical_only"}
    assert ES.config_for_search_mode("hybrid_always") == {
        "sparse_enabled": True, "sparse_mode": "always"}


def test_mode_mapping_is_case_insensitive():
    assert ES.config_for_search_mode(" HYBRID_AUTO ")["sparse_mode"] == "lexical_only"


def test_unknown_mode_rejected():
    import pytest
    for bad in ("", "bm25", None, "always"):
        with pytest.raises(ValueError):
            ES.config_for_search_mode(bad)
