"""
OpenTelemetry интеграция

Настройка трассировки запросов и метрик для:
- FastAPI приложения
- Celery задач
- Запросов к БД
"""

from typing import Optional
from loguru import logger

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from prometheus_client import start_http_server

from src.config import get_settings


def setup_opentelemetry():
    """
    Настроить OpenTelemetry для приложения.
    
    Включает:
    - Tracing (трассировка запросов)
    - Metrics (метрики производительности)
    - Resource attributes (метаданные сервиса)
    """
    settings = get_settings()
    
    # Resource с метаданными сервиса
    resource = Resource.create({
        "service.name": settings.OTEL_SERVICE_NAME,
        "service.version": "0.1.0",
        "deployment.environment": "production"
    })
    
    # Настройка Tracer
    tracer_provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(tracer_provider)
    
    # Настройка Meter
    meter_provider = MeterProvider(
        resource=resource,
        readers=[PrometheusMetricReader()]
    )
    metrics.set_meter_provider(meter_provider)
    
    # Запуск Prometheus exporter
    try:
        start_http_server(settings.OTEL_EXPORTER_PROMETHEUS_PORT)
        logger.info(f"Prometheus exporter запущен на порту {settings.OTEL_EXPORTER_PROMETHEUS_PORT}")
    except Exception as e:
        logger.warning(f"Не удалось запустить Prometheus exporter: {e}")
    
    logger.info("OpenTelemetry инициализирован")


def instrument_fastapi(app):
    """
    Инструментировать FastAPI приложение.
    
    Автоматически добавляет:
    - Tracing для всех запросов
    - Metrics (latency, request count, etc.)
    """
    try:
        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI инструментирован")
    except Exception as e:
        logger.warning(f"Не удалось инструментировать FastAPI: {e}")


def get_tracer() -> Optional[trace.Tracer]:
    """Получить tracer для ручного инструментирования"""
    try:
        return trace.get_tracer("kag-api")
    except Exception as e:
        logger.error(f"Ошибка получения tracer: {e}")
        return None


def get_meter() -> Optional[metrics.Meter]:
    """Получить meter для ручного создания метрик"""
    try:
        return metrics.get_meter("kag-api")
    except Exception as e:
        logger.error(f"Ошибка получения meter: {e}")
        return None
