"""Тесты запрета загрузки документов (переключатель админа).

Запрет должен: (1) работать при blocked=true, (2) отдавать текст причины или
дефолт, (3) НЕ блокировать систему, если настройку прочитать не удалось
(недоступная БД не должна сама включать запрет).
"""

import pytest

from src.api.services import ingest_guard as ig
from src.api.services.ingest_guard import (
    DEFAULT_MESSAGE,
    IngestBlockedError,
    ensure_ingest_allowed,
    ingest_block_message,
)

# ВАЖНО: модуль config_store надо брать через importlib. `import
# src.api.services.config_store as x` вернёт СИНГЛТОН: пакет
# src/api/services/__init__.py перекрывает атрибут пакета своим
# `from .config_store import config_store`, и обычный импорт отдаёт инстанс,
# у которого нет атрибута config_store (на этом тесты падали).
import importlib

cs_module = importlib.import_module("src.api.services.config_store")


def _patch_config(monkeypatch, value):
    monkeypatch.setattr(cs_module.config_store, "get", lambda *a, **k: value)


def test_not_blocked_returns_none(monkeypatch):
    _patch_config(monkeypatch, {"blocked": False, "message": "не важно"})
    assert ingest_block_message() is None
    ensure_ingest_allowed()          # не должно бросать


def test_empty_config_returns_none(monkeypatch):
    _patch_config(monkeypatch, {})
    assert ingest_block_message() is None


def test_blocked_returns_message(monkeypatch):
    _patch_config(monkeypatch, {"blocked": True, "message": "тестовый стенд"})
    assert ingest_block_message() == "тестовый стенд"


def test_blocked_without_message_uses_default(monkeypatch):
    _patch_config(monkeypatch, {"blocked": True})
    assert ingest_block_message() == DEFAULT_MESSAGE


def test_blocked_raises(monkeypatch):
    _patch_config(monkeypatch, {"blocked": True, "message": "стоп"})
    with pytest.raises(IngestBlockedError) as e:
        ensure_ingest_allowed()
    assert "стоп" in str(e.value)


def test_broken_config_does_not_block(monkeypatch):
    """Ошибка чтения настроек — это НЕ повод запретить загрузку."""
    def boom(*a, **k):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(cs_module.config_store, "get", boom)
    assert ingest_block_message() is None
    ensure_ingest_allowed()
