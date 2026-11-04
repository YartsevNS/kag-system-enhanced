"""
MCP инструменты

Реализация инструментов для MCP сервера:
- search_knowledge: Поиск в векторной БД
- generate_response: Генерация ответа LLM
- evaluate_quality: Оценка качества
- get_system_status: Статус системы
"""

from typing import Dict, Any, List
from loguru import logger


async def search_knowledge(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Поиск знаний в векторной БД.
    
    Параметры:
    - query: Поисковый запрос (обязательно)
    - limit: Количество результатов (опционально, по умолчанию 10)
    - filters: Фильтры по метаданным (опционально)
    
    Возвращает:
    - results: Список найденных документов
    - total: Общее количество результатов
    """
    query = params.get("query")
    limit = params.get("limit", 10)
    filters = params.get("filters", {})
    
    if not query:
        raise ValueError("Параметр 'query' обязателен")
    
    logger.info(f"Поиск: {query}, limit={limit}")

    # Честный отказ вместо пустого результата. Раньше инструмент возвращал
    # «найдено 0», и это неотличимо от «ничего не нашлось»: заказчик увидел бы
    # пустоту и решил, что поиск сломан. Настоящий поиск живёт в REST.
    raise NotImplementedError(
        "MCP search_knowledge не реализован. Используйте REST: "
        "POST /api/v1/chat/search с телом {\"query\": ..., \"limit\": ...}"
    )


async def generate_response(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Генерация ответа через LLM.
    
    Параметры:
    - messages: История сообщений (обязательно)
    - stream: Потоковая передача (опционально)
    - temperature: Температура генерации (опционально)
    - max_tokens: Максимальное количество токенов (опционально)
    
    Возвращает:
    - response: Сгенерированный ответ
    - sources: Источники (если есть)
    - metadata: Метаданные генерации
    """
    messages = params.get("messages")
    stream = params.get("stream", False)
    temperature = params.get("temperature", 0.7)
    max_tokens = params.get("max_tokens", 4096)
    
    if not messages:
        raise ValueError("Параметр 'messages' обязателен")
    
    logger.info(f"Генерация ответа, сообщений: {len(messages)}")

    raise NotImplementedError(
        "MCP generate_response не реализован. Генерация — в REST: "
        "POST /api/v1/chat (ответ строится RAG-конвейером, а не заглушкой)"
    )


async def evaluate_quality(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Оценка качества генерации.
    
    Параметры:
    - response_id: Идентификатор ответа (обязательно)
    - metrics: Метрики для оценки (опционально)
    
    Возвращает:
    - scores: Оценки по метрикам
    - feedback: Обратная связь
    """
    response_id = params.get("response_id")
    metrics = params.get("metrics", ["faithfulness", "relevance", "hallucination_rate"])
    
    if not response_id:
        raise ValueError("Параметр 'response_id' обязателен")
    
    logger.info(f"Оценка качества: {response_id}")

    # Нули здесь означали «качество идеальное или нулевое» — было не отличить.
    raise NotImplementedError(
        "MCP evaluate_quality не реализован. Оценка качества — скриптами "
        "scripts/eval_rag_judge.py и scripts/eval_retrieval.py"
    )


async def get_system_status(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Получить статус системы.
    
    Возвращает:
    - status: Общий статус
    - components: Статус компонентов
    - metrics: Метрики производительности
    """
    logger.debug("Запрос статуса системы через MCP")

    # Раньше здесь были выдуманные значения: api=running, qdrant=unknown, uptime=0.
    # По такому ответу нельзя понять, жива ли система. Теперь каждый компонент
    # проверяется по-настоящему; "not_checked" честно означает «не проверяли».
    components: Dict[str, str] = {}
    details: Dict[str, Any] = {}

    try:
        from src.indexing.embeddings_service import embeddings_service
        await embeddings_service.initialize()
        stats = await embeddings_service.get_collection_stats()
        components["qdrant"] = "ok"
        details["qdrant_points"] = (
            stats.get("points_count")
            or stats.get("vectors_count")
            or stats.get("points", 0)
        )
        details["qdrant_collection"] = stats.get("collection_name", embeddings_service.collection_name)
    except Exception as e:  # noqa: BLE001
        components["qdrant"] = f"error: {e}"

    try:
        from src.config import get_settings
        import redis as redis_lib

        s = get_settings()
        r = redis_lib.Redis(
            host=s.REDIS_HOST, port=s.REDIS_PORT, db=1,
            password=s.REDIS_PASSWORD or None, socket_timeout=2,
        )
        components["redis"] = "ok" if r.ping() else "error: ping failed"
        try:
            details["queue_depth"] = r.llen("documents")
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        components["redis"] = f"error: {e}"

    # Celery-воркеры через Redis не увидеть без брокера задач — не выдумываем.
    components["celery"] = "not_checked"
    components["mcp"] = "ok"

    checked = [v for v in components.values() if v != "not_checked"]
    status = "ok" if checked and all(v == "ok" for v in checked) else "degraded"
    return {"status": status, "components": components, "metrics": details}
