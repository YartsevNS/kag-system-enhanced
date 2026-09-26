"""Тесты бэкенда «сервис» у реранкера.

Что проверяем и почему именно это:
  * выключенный реранкер не ходит по сети вообще (иначе «выключено» будет тормозить каждый вопрос);
  * при ответе сервиса порядок берётся из ответа (иначе зачем он нужен);
  * при таймауте/ошибке порядок остаётся векторным, ответ пользователю не ломается;
  * «защёлка»: после серии неудач сервис не дёргается (иначе каждый вопрос ждёт таймаут);
  * короткий список не отправляется в сервис (нечего переставлять).
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.indexing import reranker as R  # noqa: E402


def _results(n: int = 5):
    return [{"id": f"c{i}", "content": f"фрагмент {i}", "score": 0.9 - i * 0.05} for i in range(n)]


@pytest.fixture(autouse=True)
def _reset_state():
    """Сбросить «защёлку» между тестами (иначе тесты влияют друг на друга)."""
    R._service_fails = 0
    R._service_blocked_until = 0.0
    yield
    R._service_fails = 0
    R._service_blocked_until = 0.0


def _cfg(**over):
    """Собрать конфигурацию БЕЗ вызова get_reranker_config (она в тестах подменяется — иначе рекурсия)."""
    cfg = {
        "enabled": True,
        "backend": "service",
        "model": R.DEFAULT_MODEL,
        "cache_dir": R.DEFAULT_CACHE_DIR,
        "top_k": 5,
        "endpoint": "http://test:8010",
        "timeout_ms": 300,
        "min_fragments": 4,
        "keep_dense_on_error": True,
    }
    cfg.update(over)
    return cfg


class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_disabled_backend_does_not_call_service(monkeypatch):
    calls = []
    monkeypatch.setattr(R, "get_reranker_config", lambda: _cfg(enabled=False, backend="none"))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(1))
    out = asyncio.run(R.rerank_search_results("вопрос", _results(), top_k=5))
    assert [r["id"] for r in out] == [f"c{i}" for i in range(5)]
    assert calls == [], "выключенный реранкер не должен ходить в сеть"


def test_service_order_is_applied(monkeypatch):
    monkeypatch.setattr(R, "get_reranker_config", lambda: _cfg())

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        assert body["query"] == "вопрос"
        assert len(body["candidates"]) == 5
        # сервис вернул обратный порядок
        return _Resp({"model": "test", "took_ms": 10,
                      "scores": [{"id": "i4", "score": 0.9}, {"id": "i3", "score": 0.8},
                                 {"id": "i2", "score": 0.7}, {"id": "i1", "score": 0.6},
                                 {"id": "i0", "score": 0.5}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = asyncio.run(R.rerank_search_results("вопрос", _results(), top_k=5))
    assert [r["id"] for r in out] == ["c4", "c3", "c2", "c1", "c0"], "порядок должен прийти из сервиса"


def test_timeout_keeps_dense_order_and_counts_error(monkeypatch):
    monkeypatch.setattr(R, "get_reranker_config", lambda: _cfg())

    def boom(req, timeout=None):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    out = asyncio.run(R.rerank_search_results("вопрос", _results(), top_k=5))
    assert [r["id"] for r in out] == [f"c{i}" for i in range(5)], "при таймауте порядок векторный"
    assert R._service_fails == 1, "неудача должна быть учтена"

    from src.monitoring.prometheus import reranker_errors_total
    vals = [s.value for fam in reranker_errors_total.collect() for s in fam.samples
            if s.name.endswith("_total") and s.labels.get("reason") == "TimeoutError"]
    assert vals and vals[0] >= 1, "метрика неудач реранкера не выросла"


def test_breaker_stops_calling_service_after_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(R, "get_reranker_config", lambda: _cfg())

    def boom(req, timeout=None):
        calls.append(1)
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    for _ in range(R.BREAKER_FAILS):
        asyncio.run(R.rerank_search_results("вопрос", _results(), top_k=5))
    before = len(calls)
    assert before == R.BREAKER_FAILS
    out = asyncio.run(R.rerank_search_results("вопрос", _results(), top_k=5))
    assert len(calls) == before, "после серии неудач сервис не должен вызываться (защёлка)"
    assert [r["id"] for r in out] == [f"c{i}" for i in range(5)]


def test_short_list_is_not_reranked(monkeypatch):
    calls = []
    monkeypatch.setattr(R, "get_reranker_config", lambda: _cfg(min_fragments=4))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(1))
    out = asyncio.run(R.rerank_search_results("вопрос", _results(3), top_k=5))
    assert [r["id"] for r in out] == ["c0", "c1", "c2"]
    assert calls == [], "короткий список не отправляем в сервис"


def test_incomplete_response_is_rejected(monkeypatch):
    monkeypatch.setattr(R, "get_reranker_config", lambda: _cfg())
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _Resp({"scores": [{"id": "i0", "score": 0.5}]}))
    out = asyncio.run(R.rerank_search_results("вопрос", _results(), top_k=5))
    assert [r["id"] for r in out] == [f"c{i}" for i in range(5)], "неполный ответ — векторный порядок"
    assert R._service_fails == 1
