"""Кэш LLM-извлечения: версия в ключе и переход со старого формата."""
import asyncio
import hashlib

import pytest

from src.indexing.entity_extractor import EntityExtractor, entity_extractor as ee

CFG = {"model": "m1", "provider": "p1", "extraction_mode": "two_pass", "system_prompt": "sys"}


class FakeStore:
    """Мини-замена config_store: словарь вместо Postgres."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, ns, key, default=None):
        return self.data.get(f"{ns}:{key}", default)

    def set(self, ns, key, value):
        self.data[f"{ns}:{key}"] = value

    def get_all(self, ns):
        return {k.split(":", 1)[1]: v for k, v in self.data.items() if k.startswith(f"{ns}:")}


# ── версия ключа ──────────────────────────────────────────────────────────────

def test_версия_стабильна_для_одних_настроек():
    assert EntityExtractor.cache_version(CFG) == EntityExtractor.cache_version(dict(CFG))


@pytest.mark.parametrize("change", [
    {"model": "m2"},
    {"provider": "p2"},
    {"extraction_mode": "single"},
    {"system_prompt": "другой"},
])
def test_версия_меняется_от_любой_настройки_извлечения(change):
    base = EntityExtractor.cache_version(CFG)
    assert EntityExtractor.cache_version({**CFG, **change}) != base


def test_версия_меняется_от_доменной_схемы(monkeypatch):
    base = EntityExtractor.cache_version(CFG)
    old_schema = EntityExtractor.DOMAIN_SCHEMA
    try:
        EntityExtractor.DOMAIN_SCHEMA = {"core": {"new_type": "Новый тип"}}
        assert EntityExtractor.cache_version(CFG) != base
    finally:
        EntityExtractor.DOMAIN_SCHEMA = old_schema


def test_версия_короткая():
    assert len(EntityExtractor.cache_version(CFG)) == 8


# ── размер кэша ───────────────────────────────────────────────────────────────

def test_размер_кэша_считает_только_llm_записи(monkeypatch):
    fake = FakeStore({"entity_cache:llm_a": 1, "entity_cache:llm_b": 2,
                      "entity_cache:entities_doc": 3})
    # ВАЖНО: import ... as cs даёт ЭКЗЕМПЛЯР стора, а не модуль (имя субмодуля перекрыто
    # в пакете src/api/services). Патчить надо модуль — берём его по полному пути.
    import importlib
    cs_mod = importlib.import_module("src.api.services.config_store")
    monkeypatch.setattr(cs_mod, "config_store", fake, raising=False)
    assert EntityExtractor.cache_size() == 2


# ── переход со старого ключа без обращения к LLM ─────────────────────────────

def test_старый_ключ_переиспользуется_и_переносится(monkeypatch):
    text = "Требования к средствам защиты информации в банковских технологиях"
    legacy_key = f"llm_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]}"
    cached = {"entities": [{"name": "СЗИ", "type": "legal_term"}], "relations": []}
    fake = FakeStore({f"entity_cache:{legacy_key}": cached})

    # ВАЖНО: import ... as cs даёт ЭКЗЕМПЛЯР стора, а не модуль (имя субмодуля перекрыто
    # в пакете src/api/services). Патчить надо модуль — берём его по полному пути.
    import importlib
    cs_mod = importlib.import_module("src.api.services.config_store")
    monkeypatch.setattr(cs_mod, "config_store", fake, raising=False)
    monkeypatch.setattr(ee, "_get_graph_config", lambda: dict(CFG))

    async def _boom(*a, **kw):
        raise AssertionError("LLM не должен вызываться при попадании в кэш")

    monkeypatch.setattr(ee, "_call_llm", _boom)

    res = asyncio.run(ee.extract_from_chunk(text, "chunk_1", "doc_1", "f.pdf"))
    assert res["entities"][0]["name"] == "СЗИ"
    ver = EntityExtractor.cache_version(CFG)
    new_key = f"llm_{ver}_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]}"
    assert f"entity_cache:{new_key}" in fake.data, "запись перенесена под версионный ключ"


def test_пустой_кэш_не_мешает(monkeypatch):
    text = "Достаточно длинный текст чанка для проверки отсутствия кэша"
    fake = FakeStore()
    # ВАЖНО: import ... as cs даёт ЭКЗЕМПЛЯР стора, а не модуль (имя субмодуля перекрыто
    # в пакете src/api/services). Патчить надо модуль — берём его по полному пути.
    import importlib
    cs_mod = importlib.import_module("src.api.services.config_store")
    monkeypatch.setattr(cs_mod, "config_store", fake, raising=False)
    monkeypatch.setattr(ee, "_get_graph_config", lambda: dict(CFG))

    async def _boom(*a, **kw):
        raise AssertionError("нет кэша — должен идти реальный вызов")

    monkeypatch.setattr(ee, "_call_llm", _boom)
    with pytest.raises(AssertionError):
        asyncio.run(ee.extract_from_chunk(text, "chunk_2", "doc_2", "f.pdf"))
