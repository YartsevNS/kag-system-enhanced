"""Карточка документа: префикс эмбеддинга, текст карточки, права, поиск документов."""
import json
from pathlib import Path

from src.indexing.document_card import (
    LEVEL_DOCUMENT,
    acl_payload,
    build_card_prefix,
    build_card_source,
    build_card_text,
    card_from_record,
    card_point_id,
)
from src.indexing.ids import build_embedding_text


# ── card_from_record ──────────────────────────────────────────────────────────

def test_card_from_record_темы_строкой_json():
    card = card_from_record({
        "id": "doc-1", "filename": "a.pdf", "recognized_title": "ГОСТ 1",
        "document_type": "стандарт", "summary": "о защите", "topics": '["криптография", "СЗИ"]',
    })
    assert card["topics"] == ["криптография", "СЗИ"]
    assert card["title"] == "ГОСТ 1" and card["document_type"] == "стандарт"


def test_card_from_record_пустая_запись():
    card = card_from_record(None)
    assert card["title"] == "" and card["topics"] == [] and card["summary"] == ""


# ── build_card_prefix ─────────────────────────────────────────────────────────

def test_префикс_полная_карточка():
    prefix = build_card_prefix({
        "title": "Р 1323565.1.004—2017", "document_type": "рекомендации",
        "topics": ["банковские технологии", "криптография"], "summary": "не важно",
    })
    assert prefix.startswith("Р 1323565.1.004—2017. рекомендации")
    assert "темы: банковские технологии, криптография" in prefix
    assert "не важно" not in prefix, "при наличии тем summary не добавляется"


def test_префикс_без_тем_берёт_summary():
    prefix = build_card_prefix({"title": "ГОСТ", "summary": "требования к СЗИ"})
    assert prefix == "ГОСТ. требования к СЗИ"


def test_префикс_без_названия_пустой():
    assert build_card_prefix({"title": "", "document_type": "", "topics": [], "summary": ""}) == ""


def test_префикс_служебные_типы_исключены():
    prefix = build_card_prefix({"title": "Док", "document_type": "unknown", "summary": "о чём-то"})
    assert "unknown" not in prefix


def test_префикс_ограничен_по_длине():
    prefix = build_card_prefix({"title": "Д" * 500}, max_chars=200)
    assert len(prefix) <= 200 and prefix.endswith("…")


# ── build_card_source ─────────────────────────────────────────────────────────

def test_источник_один_чанк():
    assert build_card_source([{"content": "текст"}]) == "текст"


def test_источник_берёт_начало_и_образцы():
    chunks = [{"content": f"чанк{i}"} for i in range(10)]
    src = build_card_source(chunks, max_chars=100)
    assert src.startswith("чанк0")
    assert "[фрагмент" in src, "середина/конец должны попасть образцами"
    assert len(src) <= 100


def test_источник_пустой_список():
    assert build_card_source([]) == ""


# ── build_card_text / acl / id ────────────────────────────────────────────────

def test_текст_карточки():
    text = build_card_text({"title": "ГОСТ", "document_type": "стандарт",
                            "summary": "о СЗИ", "topics": ["тема"], "filename": "a.pdf"})
    for part in ("ГОСТ", "стандарт", "о СЗИ", "Темы: тема", "Файл: a.pdf"):
        assert part in text


def test_права_доступа_из_json():
    acl = acl_payload({"visibility": "restricted", "allow_group_ids": '["g1"]',
                       "deny_user_ids": None})
    assert acl["visibility"] == "restricted"
    assert acl["allow_group_ids"] == ["g1"]
    assert acl["deny_user_ids"] == []


def test_права_по_умолчанию_public():
    assert acl_payload(None)["visibility"] == "public"


def test_id_карточки_детерминирован():
    assert card_point_id("d1") == card_point_id("d1")
    assert card_point_id("d1") != card_point_id("d2")
    assert LEVEL_DOCUMENT == "document"


# ── префикс в тексте эмбеддинга ───────────────────────────────────────────────

def test_эмбеддинг_карточка_и_структура():
    text = build_embedding_text("Требования к СЗИ", {
        "card_prefix": "Р 1323565.1.004—2017. рекомендации", "clause": "5.2.1",
    })
    assert text == "Р 1323565.1.004—2017. рекомендации · п. 5.2.1: Требования к СЗИ"


def test_эмбеддинг_только_карточка():
    text = build_embedding_text("текст", {"card_prefix": "ГОСТ. стандарт"})
    assert text == "ГОСТ. стандарт: текст"


def test_эмбеддинг_без_карточки_поведение_прежнее():
    assert build_embedding_text("текст", {"clause": "5.2.1"}) == "п. 5.2.1: текст"
    assert build_embedding_text("текст", {}) == "текст"


def test_эмбеддинг_пустой_префикс_не_добавляется():
    assert build_embedding_text("текст", {"card_prefix": "   "}) == "текст"


# ── страховки в коде конвейера и поиска ──────────────────────────────────────

def test_поиск_фрагментов_исключает_карточки():
    src = Path("src/indexing/embeddings_service.py").read_text(encoding="utf-8")
    assert 'level_must_not = [_FCL(key="level", match=_MAL(value="document"))]' in src
    assert "query_filter = QFilter(must_not=deny_must_not)" in src, \
        "исключение карточек должно применяться и без прочих условий"


def test_поиск_документов_по_уровню():
    src = Path("src/indexing/embeddings_service.py").read_text(encoding="utf-8")
    start = src.index("async def search_documents")
    body = src[start:start + 2000]
    assert 'key="level"' in body and 'value="document"' in body


def test_конвейер_анализ_до_векторизации():
    src = Path("src/api/services/document_service.py").read_text(encoding="utf-8")
    i_analyze = src.index("await self._analyze_document_async(")
    i_embed = src.index("vectors_count = await embeddings_service.embed_and_store(")
    assert i_analyze < i_embed, "карточка должна собираться ДО эмбеддинга"
    assert 'metadata["card_prefix"]' not in src  # префикс кладётся через метаданные чанка
    assert '_md["card_prefix"] = card_prefix' in src


def test_настройки_префикса_карточки():
    """Префикс — настройка, а не константа: замер показал чувствительность к длине."""
    from src.config import get_settings
    s = get_settings()
    assert isinstance(s.CARD_PREFIX_ENABLED, bool)
    assert 20 <= s.CARD_PREFIX_MAX_CHARS <= 1000


def test_короткий_префикс_без_тем():
    """Короткий вариант (title+type) не должен тянуть темы — иначе тема документа перевешивает чанк."""
    prefix = build_card_prefix(
        {"title": "СТО БР ИББС", "document_type": "стандарт",
         "topics": ["криптография", "защита"], "summary": "о чём-то"},
        max_chars=80,
    )
    assert len(prefix) <= 80


def test_чанки_документа_не_включают_карточку():
    """Карточка (level=document) не должна попадать в перестроение графа как чанк."""
    src = Path("src/indexing/embeddings_service.py").read_text(encoding="utf-8")
    i = src.index("async def get_document_chunks")
    body = src[i:i + 1600]
    assert 'key="level"' in body and 'value="document"' in body
    assert "must_not" in body

def test_acl_фильтр_карточек_использует_MatchAny():
    """MatchValue(any=...) падает с ValidationError — для списков нужен MatchAny.

    Живой случай: search_documents с непустыми group_ids ронял сравнительный режим
    («2 validation errors for MatchValue»), а карточки с ACL вообще не искались.
    """
    src = Path("src/indexing/embeddings_service.py").read_text(encoding="utf-8")
    i = src.index("async def search_documents")
    body = src[i:i + 2200]
    assert "MatchAny as _MANY" in body, "нужен импорт MatchAny"
    assert "_MANY(any=" in body, "для списков групп/пользователей — MatchAny"
    assert "_MA(any=" not in body, "MatchValue(any=...) недопустим"
