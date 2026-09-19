"""Домены: контракт хранения, заполнение при обработке и «расширение» поиска в чате.

Что проверяем и почему:
 1) домен хранится в базе (колонка + миграция) — иначе его не видно в админке и нельзя
    поправить без переиндексации (была именно такая дыра: домен жил только в payload);
 2) при обработке домен ВСЕГДА получает значение (проба по карточке, повтор, подстановка
    universal) — иначе документ выпадает из выдачи в режиме hard;
 3) точка карточки (level=document) несёт домен — раньше ровно она оставалась без домена;
 4) поиск чата расширяется без фильтра, если домен обеднил выдачу: это то, что превращало
    «факт в корпусе» в ответ «информация не найдена».
"""
import importlib

import pytest

chat_service_module = importlib.import_module("src.api.services.chat_service")
embeddings_module = importlib.import_module("src.indexing.embeddings_service")


class FakeEmbeddings:
    """Подменяет сервис эмбеддингов: считает вызовы и отдаёт заданные выдачи."""

    def __init__(self, strict, wide):
        self.strict = strict
        self.wide = wide
        self.calls = []

    async def search(self, query, limit=10, **kwargs):
        self.calls.append(kwargs.get("domain"))
        if kwargs.get("domain"):
            return list(self.strict)
        return list(self.wide)


@pytest.mark.asyncio
async def test_poisk_rasshiryaetsya_kogda_domen_obednil_vydachu(monkeypatch):
    chat = chat_service_module.ChatService.__new__(chat_service_module.ChatService)
    # режим hard: фильтр по домену строгий (config_store внутри сервиса импортируется
    # локально, поэтому подменяем сам режим, а не модуль)
    monkeypatch.setattr(chat_service_module.ChatService, "_domain_mode", lambda self: "hard")

    weak = [{"id": "weak", "score": 0.31, "content": "не то"}]
    wide = [{"id": "right", "score": 0.91, "content": "то"},
            {"id": "weak", "score": 0.31, "content": "не то"}]
    fake = FakeEmbeddings(strict=weak, wide=wide)
    monkeypatch.setattr(embeddings_module, "embeddings_service", fake)

    res = await chat_service_module.ChatService._search_with_widening(
        chat, "вопрос", 10, domain="infosec")

    # был строгий поиск и затем расширение без фильтра
    assert fake.calls == ["infosec", None], fake.calls
    ids = [c["id"] for c in res]
    assert ids[0] == "right", "лучший фрагмент должен быть первым после расширения"
    assert ids.count("weak") == 1, "дубликаты по id не должны задваиваться"
    assert res[0]["score"] > res[-1]["score"], "итог сортируется по score"


@pytest.mark.asyncio
async def test_rasshirenie_ne_vyzyvaetsya_esli_vydacha_horoshaya(monkeypatch):
    chat = chat_service_module.ChatService.__new__(chat_service_module.ChatService)
    monkeypatch.setattr(chat_service_module.ChatService, "_domain_mode", lambda self: "hard")
    good = [{"id": f"c{i}", "score": 0.9 - i / 100, "content": "x"} for i in range(10)]
    fake = FakeEmbeddings(strict=good, wide=good)
    monkeypatch.setattr(embeddings_module, "embeddings_service", fake)

    res = await chat_service_module.ChatService._search_with_widening(
        chat, "вопрос", 10, domain="infosec")
    assert fake.calls == ["infosec"], "лишний проход без фильтра не нужен"
    assert len(res) == 10


def test_domen_hranitsya_i_zapolnyaetsya():
    from pathlib import Path

    models = Path("src/database/document_models.py").read_text(encoding="utf-8")
    migrations = Path("src/database/migrations.py").read_text(encoding="utf-8")
    card = Path("src/indexing/document_card.py").read_text(encoding="utf-8")
    service = Path("src/api/services/document_service.py").read_text(encoding="utf-8")
    upload = Path("src/api/routes/upload.py").read_text(encoding="utf-8")

    assert 'domain = Column(String(32)' in models
    assert '("documents", "domain"' in migrations
    # точка карточки тоже несёт домен
    assert '"domain": (doc or {}).get("domain") or ""' in card

    # проба по карточке, повтор и явная подстановка — пустого домена быть не должно
    assert "recognized_title" in service and "_probe_card" in service
    assert 'domain = "universal"' in service
    assert '_attempt in (1, 2)' in service
    # домен пишется в базу
    assert 'get_doc_repo().upsert(document_id, {"domain": domain})' in service

    # правка названия/типа пересчитывает домен
    assert "пересчитан после правки метаданных" in upload
    assert "update_document_payload(document_id, {\"domain\": new_domain})" in upload

    # поиск чата использует расширение
    assert "_search_with_widening" in Path("src/api/services/chat_service.py").read_text(encoding="utf-8")
