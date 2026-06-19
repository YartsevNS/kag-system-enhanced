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
    
    Важно: worker — отдельный процесс, document_service загружает документы
    из config_store при инициализации. Новые документы, добавленные после
    старта worker'а, НЕ попадают в его оперативную память.
    Поэтому мы принудительно перезагружаем документ из config_store.
    """
    logger.info(f"[Celery] Начало обработки: {document_id}")
    
    try:
        # Принудительно загружаем документ из БД в память worker'а
        from src.api.services.document_service import document_service
        from src.api.services.config_store import config_store
        
        # Получаем метаданные из config_store
        all_docs = config_store.get_all("documents") or {}
        doc_data = all_docs.get(document_id)
        
        if isinstance(doc_data, str):
            raise ValueError(f"Документ повреждён в БД (строка вместо dict): {document_id}")
        
        if doc_data:
            # Пересоздаём запись в памяти (даже если уже была)
            from src.api.services.document_service import DocumentRecord
            from datetime import datetime
            
            record = DocumentRecord(
                document_id=document_id,
                filename=doc_data.get("filename", "unknown"),
                file_type=doc_data.get("file_type", ""),
                file_size=doc_data.get("file_size", 0),
                file_hash=doc_data.get("file_hash", ""),
                status=doc_data.get("status", "pending"),
                progress=doc_data.get("progress", 0),
                uploaded_by=doc_data.get("uploaded_by"),
                group_ids=doc_data.get("group_ids", []),
                version=doc_data.get("version", 1),
                created_at=datetime.fromisoformat(doc_data["created_at"]) if doc_data.get("created_at") else datetime.utcnow(),
                updated_at=datetime.fromisoformat(doc_data["updated_at"]) if doc_data.get("updated_at") else datetime.utcnow(),
            )
            document_service._documents[document_id] = record
            logger.info(f"[Celery] Документ загружен из БД: {document_id}")
        
        # Обрабатываем
        result = asyncio.run(document_service.process_document(document_id))
        logger.info(f"[Celery] ✅ Документ обработан: {document_id}")
        return {
            "document_id": document_id,
            "status": "completed",
            "result": str(result),
        }
        
    except Exception as exc:
        logger.error(f"[Celery] ❌ Ошибка обработки {document_id}: {exc}")
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


def revoke_document_tasks(document_id: str) -> int:
    """
    Отозвать все pending/active Celery задачи для указанного документа.

    Использует inspect для поиска задач по document_id в kwargs,
    затем revoke с terminate. Возвращает количество отозванных задач.
    """
    revoked = 0
    try:
        inspector = celery_app.control.inspect()
        # Смотрим active и reserved задачи
        for state_name, getter in [("active", inspector.active), ("reserved", inspector.reserved)]:
            tasks_by_worker = getter() or {}
            for worker_name, tasks in tasks_by_worker.items():
                for task in tasks:
                    kwargs = task.get("kwargs", {})
                    if kwargs.get("document_id") == document_id:
                        task_id = task.get("id")
                        if task_id:
                            celery_app.control.revoke(task_id, terminate=True)
                            logger.info(f"[revoke] {state_name} task {task_id} для {document_id}")
                            revoked += 1
        if revoked:
            logger.info(f"[revoke] Отозвано {revoked} задач для {document_id}")
    except Exception as e:
        logger.warning(f"[revoke] Ошибка при отзыве задач для {document_id}: {e}")
    return revoked

# ═══════════════════════════════════════════════════════════════
# Recovery: периодическая проверка зависших документов
# ═══════════════════════════════════════════════════════════════

@celery_app.task(
    bind=True,
    name="src.indexing.tasks.check_stuck_documents",
    queue="maintenance",
    max_retries=2,
    default_retry_delay=120,
)
def check_stuck_documents(self):
    """
    Периодическая задача (каждые 5 минут).
    Сканирует документы в статусе processing дольше порога — 
    сбрасывает в pending и перезапускает обработку.
    """
    from src.indexing.recovery import recover_stuck_documents

    logger.debug("[Beat] Проверка зависших документов...")
    try:
        result = recover_stuck_documents(requeue=True)
        if result["recovered"] > 0:
            logger.warning(
                f"[Beat] Найдено и восстановлено {result['recovered']} "
                f"зависших документов: {result['details']}"
            )
    except Exception as e:
        logger.error(f"[Beat] Ошибка проверки зависших документов: {e}")
        raise self.retry(exc=e, countdown=120)


@celery_app.task(bind=True, queue="maintenance", max_retries=3, default_retry_delay=300)
def run_monitor_check(self, source_id: str = None):
    """Запустить проверку источников мониторинга (Celery)."""
    try:
        from src.api.services.web_monitor import web_monitor
        result = asyncio.run(web_monitor.run_check(source_id))
        logger.info(f"✅ Монитор проверка: source={source_id}, results={len(result)}")
        return {"status": "ok", "checked": len(result)}
    except Exception as e:
        logger.error(f"❌ Монитор проверка упала: {e}")
        raise self.retry(exc=e)
