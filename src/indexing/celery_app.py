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
    
    # Файл расписания beat (в data-директории, доступной kag пользователю)
    beat_schedule_filename='/app/data/celerybeat-schedule',

    # Расписание периодических задач (celery beat)
    beat_schedule={
        'check-stuck-documents': {
            'task': 'src.indexing.tasks.check_stuck_documents',
            'schedule': 300.0,  # каждые 5 минут
            'options': {'queue': 'maintenance'},
        },
        'monitor-auto-check': {
            'task': 'src.indexing.tasks.run_monitor_check',
            'schedule': 21600.0,  # каждые 6 часов
            'options': {'queue': 'maintenance'},
        },
    },

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


# ================================================================
# Recovery: восстановление зависших документов при старте worker'а
# ================================================================

from celery.signals import worker_ready


@worker_ready.connect
def on_worker_ready(sender=None, **kwargs):
    try:
        from src.indexing.recovery import recover_stuck_documents
        logger.info("[Recovery] Worker ready — запуск сканера...")
        result = recover_stuck_documents(requeue=True)
        if result["recovered"] > 0:
            logger.warning(
                f"[Recovery] Восстановлено {result['recovered']} зависших документов"
            )
        else:
            logger.info("[Recovery] Зависших документов нет")
    except Exception as e:
        logger.error(f"[Recovery] Ошибка сканера: {e}")


@celery_app.task(bind=True, max_retries=5)
def debug_task(self):
    """Тестовая задача для проверки Celery"""
    logger.info(f"Celery задача работает: {self.request.id}")
    return "ok"
