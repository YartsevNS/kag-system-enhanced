"""Тесты страницы «Состояние системы» и сводки метрик.

Что проверяем и почему именно это:
- сводка отдаёт ожидаемые блоки (иначе страница молча покажет пустоту);
- записанные метрики ВИДНЫ в сводке (главный риск: счётчики пишутся, а сводка читает
  не то имя метки — тогда все плитки «—», и в /metrics это не заметно);
- квантили считаются из гистограммы (p50/p95 — то, ради чего страница нужна);
- страница отдаётся без кеша (иначе после правок видно старую версию);
- оценки ответов пишутся, мусор не принимается;
- ручка закрыта без токена (метрики системы наружу не отдаём).
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def client():
    from src.api.main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _skip_setup_gate(monkeypatch):
    """В тестах система считается настроенной.

    Без этого SetupCheckMiddleware уводит ЛЮБОЙ защищённый путь на /setup (БД в тестах
    нет) — так же делают соседние тесты (tests/test_kg_admin.py, tests/test_monitoring.py).
    """
    from src.api.middleware.setup_checker import SetupCheckMiddleware
    monkeypatch.setattr(SetupCheckMiddleware, "_is_configured", lambda self: True)


def test_overview_requires_auth(client):
    """Без токена — 401: сводка показывает внутренности системы."""
    r = client.get("/api/v1/system/overview")
    assert r.status_code == 401, r.text


def test_overview_has_expected_blocks(client, auth_headers):
    r = client.get("/api/v1/system/overview", headers=auth_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("generated_at", "uptime_seconds", "service", "requests",
                "stages", "answers", "retrieval", "llm", "feedback", "corpus"):
        assert key in d, f"нет блока {key}"
    assert d["service"]["healthy"] is True


def test_recorded_metrics_are_visible_in_overview(client, auth_headers):
    """Записали метрики — они должны появиться в сводке (проверка имён меток)."""
    from src.monitoring.prometheus import (record_answer, record_cutoff,
                                           record_feedback, record_rerank, record_rag_stage)

    record_rag_stage("llm", 2.5)
    record_answer("ok", length_chars=1200, domain="infosec", fragments=7)
    record_cutoff("infosec", dropped=3, kept=7)
    record_feedback("up")
    record_rerank(True)

    d = client.get("/api/v1/system/overview", headers=auth_headers).json()

    assert d["stages"]["llm"]["count"] >= 1, "стадия llm не видна в сводке"
    assert d["stages"]["llm"]["p95"] is not None, "квантиль не посчитан"
    assert d["answers"]["total"] >= 1
    assert d["answers"]["by_status"].get("ok", 0) >= 1
    assert d["answers"]["length_chars"]["avg"] is not None
    assert d["retrieval"]["fragments"]["count"] >= 1
    assert d["retrieval"]["cutoff_triggered"] >= 1
    assert d["retrieval"]["reranker_runs"] >= 1
    assert d["retrieval"]["reranker_top1_changed"] >= 1
    assert d["retrieval"]["reranker_change_pct"] > 0
    assert d["feedback"]["up"] >= 1


def test_quantile_interpolation():
    """Квантиль по гистограмме: без него p50/p95 на странице были бы пустыми."""
    from src.monitoring.prometheus import rag_stage_duration_seconds
    from src.api.routes.system_state import _hist_stats
    hist = rag_stage_duration_seconds.labels(stage="test-quantile")
    for v in (0.01, 0.02, 0.05, 0.10, 0.20):
        hist.observe(v)
    p50 = _hist_stats(rag_stage_duration_seconds, label_filter={"stage": "test-quantile"})
    assert p50["count"] == 5
    assert 0.03 <= p50["p50"] <= 0.07, f"p50 вне ожидаемого: {p50}"
    assert p50["p95"] >= 0.15, f"p95 вне ожидаемого: {p50}"


def test_system_page_is_served_without_cache(client, auth_headers):
    r = client.get("/system", headers=auth_headers)
    assert r.status_code == 200
    assert "Состояние системы" in r.text
    cc = r.headers.get("cache-control", "")
    assert "no-store" in cc, f"страница кешируется: {cc!r}"


def test_feedback_endpoint_records_and_rejects_garbage(client, auth_headers):
    before = client.get("/api/v1/system/feedback-stats", headers=auth_headers).json()["down"]
    r = client.post("/api/v1/system/log-feedback", headers=auth_headers,
                    json={"value": "down", "question": "тест", "answer": "текст"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["value"] == "down"
    after = client.get("/api/v1/system/feedback-stats", headers=auth_headers).json()["down"]
    assert after == before + 1, "оценка не попала в метрику"

    bad = client.post("/api/v1/system/log-feedback", headers=auth_headers,
                      json={"value": "может быть"})
    assert bad.status_code == 200
    assert bad.json()["ok"] is False, "мусорное значение не должно приниматься"


def test_health_reports_uptime(client, auth_headers):
    d = client.get("/api/v1/system/health", headers=auth_headers).json()
    assert d["status"] == "ok"
    assert d["uptime_seconds"] >= 0
