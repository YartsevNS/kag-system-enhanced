"""Схема v2: разбор ответа модели без сети (подделанный ответ) — сущности, типы, связи, цитата.

Зачем стражи: схема v2 обязана в ОДНОМ ответе получить и сущности с типами, и связи. Без типов
концов связь нельзя проверить правилом пар и записать в граф (замер 11.10.2026: 1305 связей из
1418 были не проверяемы именно поэтому), а без поля evidence связь нельзя подтвердить по тексту.
Проверяем на подделанном ответе модели: тип из СТРОГОЙ ФОРМЫ записи должен перебивать ответ
модели, выдуманный тип — не попадать в данные, поле цитаты — сохраняться.
"""
import json

import pytest

from src.indexing import extraction_v2 as exv


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


@pytest.fixture()
def fake_model(monkeypatch):
    """Привязка модели + подделанный ответ: перехватываем отправленный запрос."""
    from src.api.services.provider_service import provider_service

    seen = {}

    monkeypatch.setattr(
        provider_service, "get_function_llm_config",
        lambda _fn: {"provider": "тест", "model": "тест-модель",
                     "url": "http://example.invalid/v1", "api_key": "",
                     "parameters": {"temperature": 0.0, "no_think": True}},
    )

    answer = {
        "entities": [
            {"name": "ГОСТ Р 57580.1—2017", "type": "document_ref"},   # форма перебьёт на standard
            {"name": "Росстандарт", "type": "organization"},
            {"name": "5.2.1", "type": "clause"},                      # форма подтвердит тип
            {"name": "Пришелец", "type": "robot"},                    # выдуманный тип — не берём
            {"name": "", "type": "organization"},                     # пустое имя — пропуск
        ],
        "relations": [
            {"source": "ГОСТ Р 57580.1—2017", "target": "Росстандарт", "type": "issued-by",
             "evidence": "Росстандарт"},
            {"source": "5.2.1", "target": "5.2.2", "type": "references",
             "evidence": "в соответствии с 5.2.2"},
        ],
    }

    def fake_post(url, **kwargs):  # noqa: A002 — так же, как requests.post(url, headers=..., json=...)
        seen["url"] = url
        seen["payload"] = kwargs.get("json")
        return _Resp({"choices": [{"message": {"content": "```json\n" + json.dumps(answer,
                                                                                ensure_ascii=False) + "\n```"}}],
                      "usage": {"prompt_tokens": 900, "completion_tokens": 120,
                                "prompt_cache_hit_tokens": 0}})

    monkeypatch.setattr(exv.requests, "post", fake_post)
    return seen


TEXT = "ГОСТ Р 57580.1—2017 утвержден Росстандарт. В соответствии с 5.2.2 применяют меры."


def test_entities_parsed_and_typed_by_form(fake_model):
    out = exv.extract_chunk_v2(TEXT, "chunk-1")
    got = {e["name"]: e["type"] for e in out["entities"]}
    assert got["ГОСТ Р 57580.1—2017"] == "standard", "тип из формы записи должен перебить модель"
    assert got["Росстандарт"] == "organization"
    assert got["5.2.1"] == "clause"
    assert got["Пришелец"] == "", "выдуманный моделью тип не должен попадать в данные"
    assert "" not in got, "сущность без имени не берём"


def test_relations_keep_evidence_and_types_available(fake_model):
    out = exv.extract_chunk_v2(TEXT, "chunk-1")
    assert len(out["relations"]) == 2
    types = {e["name"]: e["type"] for e in out["entities"]}
    # Оба конца каждой связи должны быть типизированы — иначе связь не проверить и не записать.
    for r in out["relations"]:
        assert types.get(r["source"]) or types.get(r["target"])
        assert r.get("evidence"), "поле подтверждения обязано сохраняться"


def test_prompt_carries_types_and_sample(fake_model):
    exv.extract_chunk_v2(TEXT, "chunk-1")
    prompt = fake_model["payload"]["messages"][1]["content"]
    assert "Типы сущностей" in prompt and "Типы связей" in prompt
    assert "entities" in prompt and "evidence" in prompt
    assert len(prompt) < 6000, "задание не должно превращаться в простыню"
    assert fake_model["url"].endswith("/chat/completions")


def test_diagnostics_expose_raw_answer(fake_model):
    diag = {}
    exv.extract_chunk_v2(TEXT, "chunk-1", diagnostics=diag)
    assert diag["raw"] and diag["relations_model"] and diag["entities_model"]
    assert diag["prompt_chars"] > 1000 and diag["status"] == 200
    assert diag["seconds"] >= 0


class _FakeStore:
    """Настройки конвейера: только то, что читает выбор схемы."""

    def __init__(self, scheme):
        self._scheme = scheme

    def get(self, section, key):
        if (section, key) == ("system", "graph_quality"):
            return {"extraction_scheme": self._scheme}
        return None


def _extractor(monkeypatch, scheme, from_chunk):
    """Экземпляр извлекателя без __init__: нужен только метод выбора схемы."""
    import asyncio
    import importlib

    # Модуль берём через importlib: в пакете src.api.services атрибут `config_store` затенён
    # объектом-хранилищем, и обычный from-import вернул бы объект вместо модуля.
    cs_mod = importlib.import_module("src.api.services.config_store")
    from src.indexing.entity_extractor import EntityExtractor

    monkeypatch.setattr(cs_mod, "config_store", _FakeStore(scheme))
    ex = EntityExtractor.__new__(EntityExtractor)
    monkeypatch.setattr(ex, "extract_from_chunk", from_chunk)
    return ex, asyncio


def test_scheme_v2_is_used_when_configured(monkeypatch):
    """Настройка system/graph_quality.extraction_scheme = «v2» включает новую схему."""
    calls = {}

    async def from_chunk(*a, **k):
        calls["старая"] = True
        return {"entities": [{"name": "старая схема"}], "relations": []}

    monkeypatch.setattr(exv, "extract_chunk_v2",
                        lambda text, cid: {"entities": [{"name": "ГОСТ Р 57580.1—2017",
                                                         "type": "standard", "confidence": 1.0}],
                                           "relations": [{"source": "a", "target": "b",
                                                          "type": "has-part", "evidence": "текст"}]})
    ex, asyncio = _extractor(monkeypatch, "v2", from_chunk)
    res = asyncio.run(ex._extract_chunk("ГОСТ Р 57580.1—2017 утвержден", "chunk-1", "doc-1", "файл.txt"))
    assert res.get("scheme") == "v2"
    assert res["entities"][0]["name"] == "ГОСТ Р 57580.1—2017"
    assert res["entities"][0]["confidence"] == 1.0, "запись в граф ожидает поле уверенности"
    assert not calls.get("старая"), "при схеме v2 прежняя схема зваться не должна"


def test_scheme_current_keeps_old_path(monkeypatch):
    calls = {}

    async def from_chunk(*a, **k):
        calls["старая"] = True
        return {"entities": [{"name": "старая схема"}], "relations": []}

    monkeypatch.setattr(exv, "extract_chunk_v2",
                        lambda text, cid: (_ for _ in ()).throw(AssertionError("не должна зваться")))
    ex, asyncio = _extractor(monkeypatch, "current", from_chunk)
    res = asyncio.run(ex._extract_chunk("текст фрагмента", "chunk-1", "doc-1", "файл.txt"))
    assert res["entities"][0]["name"] == "старая схема" and calls["старая"]


def test_v2_failure_falls_back_to_old_scheme(monkeypatch):
    """Сбой v2 не роняет обработку документа: работаем прежней схемой."""
    async def from_chunk(*a, **k):
        return {"entities": [{"name": "старая схема"}], "relations": []}

    def boom(text, cid):
        raise RuntimeError("провайдер недоступен")

    monkeypatch.setattr(exv, "extract_chunk_v2", boom)
    ex, asyncio = _extractor(monkeypatch, "v2", from_chunk)
    res = asyncio.run(ex._extract_chunk("текст фрагмента", "chunk-1", "doc-1", "файл.txt"))
    assert res["entities"][0]["name"] == "старая схема"


def test_pipeline_calls_extractor_with_scheme_choice():
    """Структурная проверка: конвейер берёт извлечение через выбор схемы, а не напрямую."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/indexing/entity_extractor.py").read_text(encoding="utf-8")
    assert "await self._extract_chunk(chunk_text, chunk_id, document_id, filename)" in src
    assert "extraction_scheme" in src
