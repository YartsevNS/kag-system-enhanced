"""Фильтр по теме документа: читает ПОЛЕ ТЕМ, мягкий по умолчанию, легаси-домены переводятся.

Ловушка, из-за которой это появилось: чат передавал домен вопроса (из query_analysis) в поиск как
ЖЁСТКОЕ условие `domain == X`, а у документов, где детекция домена не сработала, в payload пусто —
они исчезали из выдачи, и чат отвечал «в документах не найдена» при наличии материала
(замер 18.09.2026: 1012 чанков из 4944 без домена; вопрос про 2-МР — 0.00 с фильтром против 0.90 без).

Что изменилось 10.10.2026 (и почему тесты переписаны): фильтр остался мягким, но читает уже поле ТЕМ
(`rubrics`) — коды словаря (src/indexing/document_topics.py), а значения прежней схемы
(infosec/legal/accounting/universal) переводятся в коды тем. Настройка осталась той же
(`chat/domain`), её значение — режим фильтра.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMB = (ROOT / "src/indexing/embeddings_service.py").read_text(encoding="utf-8")
CHAT = (ROOT / "src/api/services/chat_service.py").read_text(encoding="utf-8")

from src.indexing.document_topics import rubric_for_legacy  # noqa: E402
from src.indexing.embeddings_service import _theme_condition  # noqa: E402


# ── Условие фильтра ──────────────────────────────────────────────────────────

def test_жёсткий_режим_это_равенство_по_полю_тем():
    strict = _theme_condition("infosec", False)
    assert getattr(strict, "key", "") == "rubrics", "фильтр темы обязан читать поле rubrics"
    assert strict.match.value == "infosec"


def test_мягкий_режим_допускает_документы_без_темы():
    soft = _theme_condition("infosec", True)
    should = getattr(soft, "should", None)
    assert should and len(should) == 2, "мягкий режим — «тема ИЛИ пустое значение» одной выборкой"
    assert all(getattr(c, "key", "") == "rubrics" for c in should), "оба условия — по полю тем"
    values = sorted(str(c.match.value) for c in should)
    assert values == ["", "infosec"], "в условии должен быть и пустой случай"


def test_легаси_условие_осталось_для_замера():
    """«До» и «после» меряются на ОДНОМ образе: старое условие по domain сохранено для прогона."""
    assert "_legacy_domain_condition" in EMB, "без него не с чем сравнивать"
    body = EMB.split("def _legacy_domain_condition")[1][:700]
    assert 'key="domain"' in body


def test_поиск_принимает_тему_и_её_мягкость():
    assert "theme: Optional[str] = None" in EMB
    assert "theme_include_empty: bool = False" in EMB
    assert "_theme_condition(theme, theme_include_empty)" in EMB, \
        "фильтр темы должен строиться общим хелпером (оба пути: с filters и без)"


# ── Перевод значений прежней схемы ───────────────────────────────────────────

def test_легаси_домены_переводятся_в_коды_тем():
    assert rubric_for_legacy("infosec") == "infosec"
    assert rubric_for_legacy("legal") == "law"
    assert rubric_for_legacy("accounting") == "economics"


def test_универсальный_домен_не_даёт_темы():
    """«universal» — это отсутствие темы; фильтровать по нему значит отсечь почти весь корпус."""
    assert rubric_for_legacy("universal") is None
    assert rubric_for_legacy("") is None
    assert rubric_for_legacy(None) is None


def test_незнакомое_значение_не_превращается_в_тему():
    assert rubric_for_legacy("марсианский") is None
    assert rubric_for_legacy("general") is None


def test_код_словаря_проходит_как_есть():
    assert rubric_for_legacy("banking") == "banking"


# ── Режим и настройка ────────────────────────────────────────────────────────

def test_режим_читается_из_настроек_и_по_умолчанию_мягкий():
    assert "def _theme_mode(" in CHAT and "def _theme_kwargs(" in CHAT
    assert 'config_store.get("chat", "domain")' in CHAT, "режим должен браться из настроек"
    assert 'return mode if mode in ("hard", "safe", "off") else "safe"' in CHAT, (
        "неизвестное значение не должно менять поведение (остаётся безопасный режим)"
    )
    assert 'raw or "safe"' in CHAT


def test_off_и_неизвестная_тема_снимают_фильтр():
    assert 'if mode == "off":' in CHAT
    assert 'return {"theme": None}' in CHAT, "off и неопределённая тема — без фильтра"
    assert 'return {"theme": rubric, "theme_include_empty": mode == "safe"}' in CHAT


def test_расширение_выдачи_осталось():
    """Если фильтр обеднил выдачу, поиск повторяется без него — иначе снова «не найдено»."""
    assert 'if kwargs.get("theme"):' in CHAT
    assert "обеднила выдачу" in CHAT
    # подзапросы декомпозиции и стриминг по-прежнему применяют тот же режим
    assert CHAT.count("**self._theme_kwargs(") >= 2
