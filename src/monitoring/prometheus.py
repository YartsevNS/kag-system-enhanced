"""
Prometheus метрики

Определение кастомных метрик для:
- Количества запросов
- Времени ответа
- Ошибок
- Размера данных
"""

from prometheus_client import Counter, Histogram, Gauge, Info
from loguru import logger

# Метрики запросов
http_requests_total = Counter(
    "http_requests_total",
    "Общее количество HTTP запросов",
    ["method", "endpoint", "status"]
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "Время ответа HTTP запроса",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0]
)

# Метрики ошибок
http_errors_total = Counter(
    "http_errors_total",
    "Общее количество HTTP ошибок",
    ["method", "endpoint", "error_type"]
)

# Метрики LLM
llm_requests_total = Counter(
    "llm_requests_total",
    "Общее количество запросов к LLM",
    ["model", "status"]
)

llm_request_duration_seconds = Histogram(
    "llm_request_duration_seconds",
    "Время генерации ответа LLM",
    ["model"],
    buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "Общее количество обработанных токенов",
    ["type"]  # prompt, completion
)

# Метрики векторной БД
qdrant_operations_total = Counter(
    "qdrant_operations_total",
    "Общее количество операций с Qdrant",
    ["operation", "status"]
)

qdrant_operation_duration_seconds = Histogram(
    "qdrant_operation_duration_seconds",
    "Время операций с Qdrant",
    ["operation"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0]
)

qdrant_vectors_count = Gauge(
    "qdrant_vectors_count",
    "Количество векторов в коллекции",
    ["collection"]
)

# Метрики Celery
celery_tasks_total = Counter(
    "celery_tasks_total",
    "Общее количество задач Celery",
    ["task", "status"]
)

celery_task_duration_seconds = Histogram(
    "celery_task_duration_seconds",
    "Время выполнения задач Celery",
    ["task"],
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0]
)

celery_queue_size = Gauge(
    "celery_queue_size",
    "Размер очереди Celery",
    ["queue"]
)

# Метрики кэша
cache_operations_total = Counter(
    "cache_operations_total",
    "Общее количество операций кэша",
    ["operation", "status"]
)

cache_hit_ratio = Gauge(
    "cache_hit_ratio",
    "Коэффициент попаданий в кэш"
)

# Метрики документов
documents_processed_total = Counter(
    "documents_processed_total",
    "Общее количество обработанных документов",
    ["file_type", "status"]
)

documents_processing_duration_seconds = Histogram(
    "documents_processing_duration_seconds",
    "Время обработки документов",
    ["file_type"],
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 300.0]
)

# Информация о сервисе
service_info = Info(
    "service_info",
    "Информация о сервисе"
)


def setup_prometheus_metrics():
    """Настроить начальные значения метрик"""
    service_info.info({
        "name": "kag-api",
        "version": "0.1.0",
        "description": "Knowledge Augmentation Generation API"
    })
    
    logger.info("Prometheus метрики инициализированы")


def record_http_request(method: str, endpoint: str, status: int, duration: float):
    """Записать HTTP запрос"""
    http_requests_total.labels(method=method, endpoint=endpoint, status=status).inc()
    http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)


def record_http_error(method: str, endpoint: str, error_type: str):
    """Записать HTTP ошибку"""
    http_errors_total.labels(method=method, endpoint=endpoint, error_type=error_type).inc()


def record_llm_request(model: str, status: str, duration: float, tokens_prompt: int, tokens_completion: int):
    """Записать запрос к LLM"""
    llm_requests_total.labels(model=model, status=status).inc()
    llm_request_duration_seconds.labels(model=model).observe(duration)
    llm_tokens_total.labels(type="prompt").inc(tokens_prompt)
    llm_tokens_total.labels(type="completion").inc(tokens_completion)


def record_qdrant_operation(operation: str, status: str, duration: float):
    """Записать операцию с Qdrant"""
    qdrant_operations_total.labels(operation=operation, status=status).inc()
    qdrant_operation_duration_seconds.labels(operation=operation).observe(duration)


def record_celery_task(task: str, status: str, duration: float):
    """Записать задачу Celery"""
    celery_tasks_total.labels(task=task, status=status).inc()
    celery_task_duration_seconds.labels(task=task).observe(duration)


def record_document_processing(file_type: str, status: str, duration: float):
    """Записать обработку документа"""
    documents_processed_total.labels(file_type=file_type, status=status).inc()
    documents_processing_duration_seconds.labels(file_type=file_type).observe(duration)
