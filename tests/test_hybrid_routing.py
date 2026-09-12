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
