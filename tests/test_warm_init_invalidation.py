"""Тёплая инициализация эмбеддингов: окно переинициализации закрыто подписью модели.

Зачем тест: initialize() зовётся на каждый запрос (чат, /meta, /access, сторож), и
раньше каждый вызов платил ~163 мс за пробный эмбеддинг. Теперь есть тёплое
состояние — и важно, чтобы оно сбрасывалось там, где это нужно: по таймауту и при
смене НАСТРОЙКИ модели (иначе после смены модели поиск до 60 с шёл бы в коллекцию
старой размерности).
"""
import time

from src.indexing.embeddings_service import EmbeddingsService


def _service(signature="prov1|model-a|model-a|http://embed"):
    svc = EmbeddingsService.__new__(EmbeddingsService)   # без Qdrant/клиентов
    svc.INIT_CHECK_INTERVAL = 60.0
    svc._ready_at = time.monotonic()
    svc._model_signature = signature
    return svc


def test_warm_state_skips_reinit(monkeypatch):
    svc = _service()
    monkeypatch.setattr(EmbeddingsService, "_current_model_signature", staticmethod(lambda: svc._model_signature))
    assert svc._needs_reinit() is False, "тёплое состояние должно пропускать работу"


def test_model_change_forces_reinit(monkeypatch):
    svc = _service(signature="prov1|model-a|model-a|http://embed")
    monkeypatch.setattr(EmbeddingsService, "_current_model_signature", staticmethod(lambda: "prov1|model-b|model-b|http://embed"))
    assert svc._needs_reinit() is True, "смена модели должна переинициализировать сразу, без окна 60 с"


def test_interval_expiry_forces_reinit(monkeypatch):
    svc = _service()
    svc._ready_at = time.monotonic() - svc.INIT_CHECK_INTERVAL - 1
    monkeypatch.setattr(EmbeddingsService, "_current_model_signature", staticmethod(lambda: svc._model_signature))
    assert svc._needs_reinit() is True, "по истечении окна состояние перепроверяется"


def test_cold_service_needs_reinit():
    svc = _service()
    svc._ready_at = 0.0
    assert svc._needs_reinit() is True, "до первой инициализации работа нужна"


def test_invalidate_resets_warm_state():
    svc = _service()
    svc.invalidate_initialization()
    assert svc._ready_at == 0.0, "invalidate_initialization() должен сбросить тёплое состояние"
