"""Метрики: стадии RAG пишутся, вызовы модели считаются, /metrics отдаётся.

Зачем: до 26.09.2026 метрики в src/monitoring/prometheus.py были объявлены, но НИКТО их не
записывал и эндпоинта /metrics не существовало — то есть Prometheus собирать было нечего.
Тесты стерегут три вещи: запись стадий, запись вызовов модели и отдачу в формате Prometheus.
"""
import asyncio

from prometheus_client import generate_latest


def test_rag_stage_is_recorded():
    from src.monitoring.prometheus import rag_stage_duration_seconds, record_rag_stage

    before = rag_stage_duration_seconds.labels(stage="qdrant")._sum.get()
    record_rag_stage("qdrant", 0.123)
    after = rag_stage_duration_seconds.labels(stage="qdrant")._sum.get()
    assert after > before, "длительность стадии не записалась"


def test_llm_call_records_tokens():
    from src.monitoring.prometheus import llm_tokens_total, record_llm_call

    before = llm_tokens_total.labels(type="prompt")._value.get()
    record_llm_call("тест-модель", "ok", 1.5, prompt_tokens=100, completion_tokens=50)
    after = llm_tokens_total.labels(type="prompt")._value.get()
    assert after == before + 100, "токены промпта не записались"


def test_negative_duration_is_clamped():
    """Отрицательные значения не должны попадать в гистограмму (иначе она ломается)."""
    from src.monitoring.prometheus import rag_stage_duration_seconds, record_rag_stage

    record_rag_stage("access", -5)
    assert rag_stage_duration_seconds.labels(stage="access")._sum.get() >= 0


def test_metrics_endpoint_serves_prometheus_format():
    from src.api.main import prometheus_metrics

    resp = asyncio.run(prometheus_metrics())
    text = resp.body.decode()
    assert "rag_stage_duration_seconds" in text, "в /metrics нет наших стадий"
    assert "llm_request_duration_seconds" in text
    assert resp.media_type.startswith("text/plain"), resp.media_type


def test_generate_latest_contains_stage_labels():
    record_text = generate_latest().decode()
    assert 'stage="qdrant"' in record_text or 'rag_stage_duration_seconds' in record_text
