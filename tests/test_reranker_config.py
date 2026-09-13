"""Реранкер: настройка из админки, выключатель, честный статус.

Живой контекст (2026-09-13): модель была захардкожена как «BAAI/bge-reranker-v2-m3», которой
у flashrank нет → 404 → реранкер не загружался никогда; вместо явного «выключено» молча
включался BM25-фолбэк, падал с division by zero и возвращал исходный порядок. Тесты фиксируют
новое поведение: «выключено» = выключено, модель выбирается из списка, статус виден.
"""
import importlib

import pytest

from src.indexing import reranker as rr


class FakeStore:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, ns, key, default=None):
        return self.data.get(f"{ns}:{key}", default)

    def set(self, ns, key, value):
        self.data[f"{ns}:{key}"] = value


def _patch_store(monkeypatch, data):
    # ВАЖНО: import src.api.services.config_store as cs даёт ЭКЗЕМПЛЯР, а не модуль
    # (имя субмодуля перекрыто в пакете) — берём модуль по полному пути.
    cs_mod = importlib.import_module("src.api.services.config_store")
    monkeypatch.setattr(cs_mod, "config_store", FakeStore(data), raising=False)


# ── настройки ────────────────────────────────────────────────────────────────

def test_по_умолчанию_выключен(monkeypatch):
    _patch_store(monkeypatch, {})
    cfg = rr.get_reranker_config()
    assert cfg["enabled"] is False, "по умолчанию реранкер выключен — включается в админке"
    assert cfg["model"] == rr.DEFAULT_MODEL
    assert cfg["cache_dir"].startswith("/app/data/"), "кэш в постоянном каталоге, не в overlay"


def test_настройки_читаются(monkeypatch):
    _patch_store(monkeypatch, {"reranker:config": {
        "enabled": True, "model": "ms-marco-MiniLM-L-12-v2",
        "cache_dir": "/tmp/rr", "top_k": 3,
    }})
    cfg = rr.get_reranker_config()
    assert cfg["enabled"] is True
    assert cfg["model"] == "ms-marco-MiniLM-L-12-v2"
    assert cfg["cache_dir"] == "/tmp/rr"
    assert cfg["top_k"] == 3


def test_мультиязычная_модель_в_списке():
    assert "ms-marco-MultiBERT-L-12" in rr.SUPPORTED_MODELS
    assert rr.DEFAULT_MODEL == "ms-marco-MultiBERT-L-12", \
        "по умолчанию мультиязычная: остальные модели flashrank англоязычные"
    assert "BAAI/bge-reranker-v2-m3" not in rr.SUPPORTED_MODELS, \
        "этой модели у flashrank нет — именно она давала 404"


def test_статус_для_админки(monkeypatch):
    _patch_store(monkeypatch, {})
    st = rr.reranker_status()
    for key in ("enabled", "model", "cache_dir", "available_models", "loaded", "load_error"):
        assert key in st
    assert st["available_models"] == rr.SUPPORTED_MODELS


# ── поведение при выключенном реранкере ──────────────────────────────────────

def test_выключенный_не_подменяется_фолбэком(monkeypatch):
    _patch_store(monkeypatch, {"reranker:config": {"enabled": False}})
    rr._reload()
    assert rr.get_default_reranker() is None, "выключено — значит никакого BM25 под капотом"


def test_выключенный_не_меняет_порядок(monkeypatch):
    import asyncio
    _patch_store(monkeypatch, {"reranker:config": {"enabled": False}})
    rr._reload()
    results = [{"content": "а", "score": 0.9}, {"content": "б", "score": 0.8},
               {"content": "в", "score": 0.7}]
    out = asyncio.run(rr.rerank_search_results("запрос", results, top_k=2))
    assert [r["content"] for r in out] == ["а", "б"], "порядок исходный, обрезка по top_k"


def test_bm25_не_падает_на_пустом_словаре():
    bm = rr._BM25_Reranker()
    passages = [{"content": "совсем другой текст"}, {"content": "и тут другое"}]
    out = bm.rerank("абракадабра-которой-нет", passages, top_k=2)
    assert len(out) == 2, "division by zero не должен ронять реранкер (живой случай)"


def test_пустой_запрос_не_ломает():
    bm = rr._BM25_Reranker()
    assert len(bm.rerank("", [{"content": "текст"}], top_k=1)) == 1


# ── проводка в админку ───────────────────────────────────────────────────────

def test_эндпоинты_есть():
    src = open("src/api/routes/admin_models.py", encoding="utf-8").read()
    assert '/reranker-config"' in src or "/reranker-config" in src
    assert "reranker_status" in src and "warm_reranker" in src
    assert "SUPPORTED_MODELS" in src, "неизвестная модель должна отбиваться 422 со списком"


def test_карточка_в_интерфейсе():
    html = open("src/api/static/admin.html", encoding="utf-8").read()
    for marker in ("reranker-enabled", "reranker-model", "reranker-status",
                   "loadRerankerConfig", "saveRerankerConfig", "warmRerankerModel"):
        assert marker in html, marker
    assert "loadRerankerConfig();" in html, "настройки должны грузиться при открытии страницы"


def test_оценка_реранкера_обычный_float():
    """numpy.float32 в ответе ломает сериализацию API (живой случай: пустые ответы чата)."""
    import asyncio
    import sys
    import types

    src = open("src/indexing/reranker.py", encoding="utf-8").read()
    assert 'float(r.get("score"' in src, "оценка реранкера обязана приводиться к float"
    assert "Реранкер — УЛУЧШЕНИЕ" in src, "получение ранкера должно быть внутри try"


def test_битый_реранкер_не_ломает_ответ(monkeypatch):
    import asyncio
    _patch_store(monkeypatch, {"reranker:config": {"enabled": True}})

    def _boom():
        raise RuntimeError("модель сломалась")

    monkeypatch.setattr(rr, "get_default_reranker", _boom)
    out = asyncio.run(rr.rerank_search_results("запрос", [{"content": "а"}], top_k=1))
    assert out and out[0]["content"] == "а", "ошибка реранкера не должна ронять поиск"
