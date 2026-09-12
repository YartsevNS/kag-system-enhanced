"""Тесты русского лексического анализатора для sparse-ветки (BM25).

Проверяем то, из-за чего ветка проигрывала плотному поиску: падежные формы,
частотные слова-шум и ё/е; плюс что номера документов и ГОСТы не портятся.
"""

from src.indexing.lexical import (
    STOPWORDS_RU,
    normalize,
    sparse_terms,
    sparse_vector,
    stem_ru,
    tokenize_lexical,
)


def test_yo_normalized():
    assert normalize("Всё Ёлка") == "все елка"
    assert tokenize_lexical("Всё") == tokenize_lexical("все")


def test_stopwords_dropped():
    tokens = tokenize_lexical("и в на требования к защите информации")
    assert "и" not in tokens and "в" not in tokens and "на" not in tokens
    assert any(t.startswith("требо") for t in tokens), tokens
    assert "к" not in tokens


def test_case_forms_share_stem():
    """«получение» и «получить» должны дать общий терм (усечение длинных основ)."""
    a = sparse_terms("получение вычета")
    b = sparse_terms("получить вычет")
    verbs_a = set(a) - {"вычет"}
    verbs_b = set(b) - {"вычет"}
    assert stem_ru("получение") == stem_ru("получить"), (
        f"разные основы: {stem_ru('получение')} vs {stem_ru('получить')}")
    assert verbs_a & verbs_b, f"нет общих глагольных термов: {verbs_a} / {verbs_b}"


def test_declension_merge():
    one = set(sparse_terms("требования к защите").keys())
    two = set(sparse_terms("требование защита").keys())
    assert one & two, "падежные формы не сходятся"


def test_numbers_and_standards_untouched():
    """Номера документов и стандартов ищутся точно — их не стеммим."""
    tokens = tokenize_lexical("ГОСТ Р 56545-2015 приказ 7403")
    assert "56545" in tokens
    assert "7403" in tokens
    assert "гост" in tokens
    assert stem_ru("2026") == "2026"


def test_short_words_not_overstemmed():
    """Односложные/короткие слова не должны схлопываться в пустую строку."""
    for word in ("ось", "оси", "дом", "уши"):
        assert len(stem_ru(word)) >= 2


def test_vector_deterministic_and_stable():
    text = "Требования к средствам защиты информации в финансовых организациях"
    assert sparse_vector(text) == sparse_vector(text)
    assert sparse_vector(text)["indices"], "вектор пустой"
    assert len(sparse_vector(text)["indices"]) == len(sparse_vector(text)["values"])


def test_different_texts_differ():
    a = set(sparse_vector("требования к защите информации")["indices"])
    b = set(sparse_vector("инфляция и потребительские цены")["indices"])
    assert a != b and not (a & b)


def test_stopwords_list_is_sane():
    assert len(STOPWORDS_RU) > 100
    assert all(w == w.lower() for w in STOPWORDS_RU)
    assert "и" in STOPWORDS_RU and "который" in STOPWORDS_RU
