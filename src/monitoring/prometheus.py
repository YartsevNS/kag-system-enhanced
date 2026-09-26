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


# ── Стадии RAG-запроса (2026-09-26) ─────────────────────────────────────────────
# Зачем отдельная гистограмма: по ней видно, куда уходит время внутри одного вопроса —
# поиск в Qdrant, граф, табличный слой, вызов модели. Метки по стадии, чтобы в Grafana
# строить p50/p95/p99 по каждой.
rag_stage_duration_seconds = Histogram(
    "rag_stage_duration_seconds",
    "Длительность стадий обработки вопроса (mode=qdrant|graph|tables|llm|access)",
    ["stage"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
)


def record_rag_stage(stage: str, duration: float) -> None:
    """Записать длительность стадии вопроса (в секундах). Ошибки метрик не должны ломать ответ."""
    try:
        rag_stage_duration_seconds.labels(stage=stage).observe(max(duration, 0.0))
    except Exception:
        pass


def record_llm_call(model: str, status: str, duration: float,
                    prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    """Записать вызов модели: счётчик, время и токены (для контроля стоимости)."""
    try:
        llm_requests_total.labels(model=model or "unknown", status=status).inc()
        llm_request_duration_seconds.labels(model=model or "unknown").observe(max(duration, 0.0))
        if prompt_tokens:
            llm_tokens_total.labels(type="prompt").inc(prompt_tokens)
        if completion_tokens:
            llm_tokens_total.labels(type="completion").inc(completion_tokens)
    except Exception:
        pass


# ── Качество ответов и поведение отбора (2026-09-26) ───────────────────────────
# Зачем: операционные метрики (время, токены) не отвечают на вопрос «а ответ-то
# хороший?». Эти пять отвечают: сколько ответов, сколько отказов и почему, сколько
# фрагментов уходит в промпт, как часто порог отсекает лишнее, менял ли реранкер
# лучший фрагмент, что говорят сами пользователи.
answers_total = Counter(
    "rag_answers_total",
    "Ответы чата по итогу (ok | refusal_low_score | refusal_no_data | error)",
    ["status"]
)

answer_length_chars = Histogram(
    "rag_answer_length_chars",
    "Длина ответа в символах (аномально короткие = проблема с контекстом)",
    buckets=[100, 300, 600, 1200, 2500, 5000, 10000, 25000]
)

context_fragments = Histogram(
    "rag_context_fragments",
    "Сколько фрагментов ушло в промпт",
    ["domain"],
    buckets=[1, 2, 3, 5, 8, 10, 15, 20, 30]
)

cutoff_triggered_total = Counter(
    "rag_cutoff_triggered_total",
    "Ответы, где порог релевантности отсеял хотя бы один фрагмент",
    ["domain"]
)

cutoff_dropped_fragments = Histogram(
    "rag_cutoff_dropped_fragments",
    "Сколько фрагментов отсеял порог релевантности за один ответ",
    ["domain"],
    buckets=[0, 1, 2, 3, 5, 8, 13, 20, 35]
)

feedback_total = Counter(
    "rag_feedback_total",
    "Оценки ответов пользователями (up | down)",
    ["value"]
)

reranker_runs_total = Counter(
    "rag_reranker_runs_total",
    "Сколько раз реранкер пересортировал выдачу"
)

reranker_changed_top1_total = Counter(
    "rag_reranker_changed_top1_total",
    "Сколько раз реранкер поменял лучший (первый) фрагмент"
)

reranker_errors_total = Counter(
    "rag_reranker_errors_total",
    "Неудачи реранкера по причине (timeout | URLError | неполный_ответ | ...)",
    ["reason"]
)


def record_reranker_error(reason: str) -> None:
    """Записать неудачу реранкера: сервис не ответил, таймаут, неполный ответ.

    Зачем отдельно: при бэкенде «сервис» отказ внешнего процесса выглядит для пользователя как
    обычный ответ (порядок остался векторным), и без этой метрики отказ вообще не видно.
    """
    try:
        reranker_errors_total.labels(reason=str(reason or "unknown")[:40]).inc()
    except Exception:
        pass


def record_answer(status: str, length_chars: int = 0, domain: str = "",
                  fragments: int = 0) -> None:
    """Записать итог ответа: статус, длину, домен и число фрагментов в промпте."""
    try:
        answers_total.labels(status=status).inc()
        if length_chars > 0:
            answer_length_chars.observe(length_chars)
        if fragments > 0:
            context_fragments.labels(domain=domain or "unknown").observe(fragments)
    except Exception:
        pass


def record_cutoff(domain: str, dropped: int, kept: int) -> None:
    """Записать работу порога отсечения: сколько фрагментов убрали и сколько оставили."""
    try:
        _d = domain or "unknown"
        cutoff_dropped_fragments.labels(domain=_d).observe(max(dropped, 0))
        if dropped > 0:
            cutoff_triggered_total.labels(domain=_d).inc()
    except Exception:
        pass


def record_feedback(value: str) -> None:
    """Записать оценку ответа пользователем (up | down)."""
    try:
        feedback_total.labels(value=value if value in ("up", "down") else "other").inc()
    except Exception:
        pass


def record_rerank(changed_top1: bool) -> None:
    """Записать работу реранкера: применялся и поменял ли лучший фрагмент."""
    try:
        reranker_runs_total.inc()
        if changed_top1:
            reranker_changed_top1_total.inc()
    except Exception:
        pass
