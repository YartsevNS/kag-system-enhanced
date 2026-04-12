"""
Celery приложение для фоновой обработки задач

Используется для:
- Пакетной индексации документов
- Транскрипции аудио
- Векторизации и чанкинга
- Очистки кэша
"""

from celery import Celery
from loguru import logger

from src.config import get_settings

settings = get_settings()

# Создание Celery приложения
celery_app = Celery(
    "kag_indexing",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

# Конфигурация Celery
celery_app.conf.update(
    # Сериализация
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    
    # Временная зона
    timezone="UTC",
    enable_utc=True,
    
    # Маршрутизация задач
    task_routes={
        "src.indexing.tasks.process_document": {"queue": "documents"},
        "src.indexing.tasks.transcribe_audio": {"queue": "audio"},
        "src.indexing.tasks.vectorize_document": {"queue": "vectorization"},
        "src.indexing.tasks.clear_cache_task": {"queue": "maintenance"},
    },
    
    # Повторные попытки
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    
    # Лимиты задач
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
    
    # Таймауты
    task_soft_time_limit=3600,  # 1 час
    task_time_limit=7200,  # 2 часа
    
    # Ретраи с экспоненциальной задержкой
    task_default_retry_delay=60,
    task_default_max_retries=5,
)

# Автообнаружение задач
celery_app.autodiscover_tasks(["src.indexing"])


@celery_app.task(bind=True, max_retries=5)
def debug_task(self):
    """Тестовая задача для проверки Celery"""
    logger.info(f"Celery задача работает: {self.request.id}")
    return "ok"
