"""
Celery задачи для обработки документов.

Вызываются из upload.py вместо asyncio.Queue.
Преимущества: retry, изоляция, очередь в Redis (не теряется при перезапуске).
"""

from typing import Dict, Any, Optional
import asyncio
from loguru import logger

from src.indexing.celery_app import celery_app
from src.api.services.document_service import document_service


@celery_app.task(
    bind=True,
    queue="documents",
    max_retries=5,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_document(
    self,
    document_id: str,
) -> Dict[str, Any]:
    """
    Обработать документ: парсинг → чанкинг → векторизация.
    Выполняется в Celery worker (отдельный контейнер).
    
    Args:
        document_id: Идентификатор документа
        
    Returns:
        Результат обработки
        
    Raises:
        self.retry: при любой ошибке (5 попыток, экспоненциальная задержка)
    """
    logger.info(f"[Celery] Начало обработки: {document_id}")
    
    try:
        # document_service.process_document — async, запускаем через asyncio.run
        result = asyncio.run(document_service.process_document(document_id))
        logger.info(f"[Celery] ✅ Документ обработан: {document_id}")
        return {
            "document_id": document_id,
            "status": "completed",
            "result": str(result),
        }
        
    except Exception as exc:
        logger.error(f"[Celery] ❌ Ошибка обработки {document_id}: {exc}")
        
        # Экспоненциальная задержка: 60с, 120с, 240с, 480с, 960с
        countdown = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(
    bind=True,
    queue="vectorization",
    max_retries=3
)
def vectorize_document(
    self,
    document_id: str,
    chunks: list,
    metadata: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Векторизовать чанки и сохранить в Qdrant.
    
    Args:
        document_id: Идентификатор документа
        chunks: Список чанков текста
        metadata: Метаданные документа
        
    Returns:
        Результат векторизации
    """
    logger.info(f"Векторизация документа: {document_id}")
    
    try:
        vectorizer = Vectorizer()
        result = vectorizer.vectorize(document_id, chunks, metadata)
        
        logger.info(f"Документ векторизован: {document_id}, добавлено: {result.get('added', 0)}")
        
        return {
            "document_id": document_id,
            "status": "completed",
            "vectors_added": result.get("added", 0)
        }
        
    except Exception as exc:
        logger.error(f"Ошибка векторизации {document_id}: {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@celery_app.task(
    bind=True,
    queue="audio",
    max_retries=3
)
def transcribe_audio(
    self,
    document_id: str,
    audio_path: str,
    language: str = "ru"
) -> Dict[str, Any]:
    """
    Транскрибировать аудиофайл через Whisper.
    
    Args:
        document_id: Идентификатор документа
        audio_path: Путь к аудиофайлу
        language: Язык аудио
        
    Returns:
        Результат транскрипции
    """
    logger.info(f"Транскрипция аудио: {document_id}")
    
    try:
        # TODO: Интеграция с Whisper
        # whisper_model = whisper.load_model("base")
        # result = whisper_model.transcribe(audio_path, language=language)
        
        # Заглушка для демонстрации
        result = {
            "text": "Транскрипция будет реализрована позже",
            "segments": []
        }
        
        logger.info(f"Аудио транскрибировано: {document_id}")
        
        return {
            "document_id": document_id,
            "status": "transcribed",
            "text_length": len(result.get("text", ""))
        }
        
    except Exception as exc:
        logger.error(f"Ошибка транскрипции {document_id}: {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@celery_app.task(
    bind=True,
    queue="maintenance",
    max_retries=1
)
def clear_cache_task(self, cache_pattern: str = "*") -> Dict[str, Any]:
    """
    Очистить кэш в Redis.
    
    Args:
        cache_pattern: Паттерн для удаления ключей
        
    Returns:
        Результат очистки
    """
    logger.warning(f"Очистка кэша по паттерну: {cache_pattern}")
    
    try:
        import redis
        from src.config import get_settings
        
        settings = get_settings()
        r = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD
        )
        
        # Находим ключи по паттерну
        keys = r.keys(cache_pattern)
        
        if keys:
            deleted = r.delete(*keys)
            logger.info(f"Удалено ключей из кэша: {deleted}")
            return {"deleted": deleted}
        
        return {"deleted": 0}
        
    except Exception as exc:
        logger.error(f"Ошибка очистки кэша: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    queue="documents",
    max_retries=3
)
def batch_process_documents(
    self,
    documents: list[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Пакетная обработка нескольких документов.
    
    Args:
        documents: Список документов для обработки
        
    Returns:
        Результат пакетной обработки
    """
    logger.info(f"Пакетная обработка: {len(documents)} документов")
    
    results = []
    for doc in documents:
        try:
            result = process_document.delay(
                document_id=doc["document_id"],
                file_path=doc["file_path"],
                file_type=doc["file_type"],
                metadata=doc.get("metadata")
            )
            results.append({
                "document_id": doc["document_id"],
                "task_id": result.id,
                "status": "queued"
            })
        except Exception as e:
            logger.error(f"Ошибка постановки в очередь {doc['document_id']}: {e}")
            results.append({
                "document_id": doc["document_id"],
                "status": "failed",
                "error": str(e)
            })
    
    return {
        "total": len(documents),
        "queued": sum(1 for r in results if r["status"] == "queued"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results
    }
