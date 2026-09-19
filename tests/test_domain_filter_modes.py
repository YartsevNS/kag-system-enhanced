"""Режимы домена в поиске чата: жёсткий фильтр скрывал документы без домена.

Ловушка, из-за которой это появилось: chat передавал домен вопроса (из query_analysis)
в поиск как ЖЁСТКОЕ условие `domain == X`. У документов, где детекция домена при
индексации не сработала, в payload пустая строка — они не проходили фильтр и исчезали
из выдачи, а чат отвечал «в документах не найдена» при наличии материала
(замер 18.09.2026: 1012 чанков из 4944 без домена; вопрос про 2-МР — 0.00 с фильтром
против 0.90 без).

Проверки: (1) «мягкое» условие допускает пустой домен и делается ОДНИМ запросом
(пустую строку ловит MatchValue(""), а не is_empty — проверено на живом Qdrant:
is_empty=81, пустая строка=1012); (2) режим читается из настройки chat/domain и по
умолчанию остаётся прежним (hard) — поведение меняется только по замеру.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMB = (ROOT / "src/indexing/embeddings_service.py").read_text(encoding="utf-8")
CHAT = (ROOT / "src/api/services/chat_service.py").read_text(encoding="utf-8")


def test_domain_condition_includes_empty_domain():
    from src.indexing.embeddings_service import _domain_condition

    strict = _domain_condition("infosec", False)
    assert getattr(strict, "key", "") == "domain", "жёсткий режим — простое равенство домена"
    assert strict.match.value == "infosec"

    soft = _domain_condition("infosec", True)
    should = getattr(soft, "should", None)
    assert should and len(should) == 2, "мягкий режим — «домен ИЛИ пустой» одной выборкой"
    values = sorted(str(c.match.value) for c in should)
    assert values == ["", "infosec"], "в условии должен быть и пустой домен"


def test_search_accepts_domain_include_empty_flag():
    assert "domain_include_empty: bool = False" in EMB, "у поиска должен быть флаг мягкого домена"
    assert "_domain_condition(domain, domain_include_empty)" in EMB, (
        "фильтр домена должен строиться через общий хелпер (оба пути: с filters и без)"
    )


def test_chat_domain_modes_read_from_settings():
    assert "def _domain_mode(" in CHAT and "def _domain_kwargs(" in CHAT
    assert 'config_store.get("chat", "domain")' in CHAT, "режим должен браться из настроек"
    assert 'return mode if mode in ("hard", "safe", "off") else "hard"' in CHAT, (
        "неизвестное значение не должно менять поведение"
    )
    # по умолчанию прежнее поведение: включаем новое только по замеру
    assert 'raw or "hard"' in CHAT
    # все места, где чат ищет фрагменты, обязаны уважать режим
    assert CHAT.count("**self._domain_kwargs(") >= 3, (
        "основной поиск, подзапросы декомпозиции и стриминг — все через режим домена"
    )
