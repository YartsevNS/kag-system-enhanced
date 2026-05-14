"""
Celery задачи для обработки документов
"""

from typing import Dict, Any, Optional
from celery import chain
from loguru import logger
import time

from src.indexing.celery_app import celery_app
from src.indexing.parsers import DocumentParser
from src.indexing.chunking import DocumentChunker
from src.indexing.vectorizer import Vectorizer


@celery_app.task(
    bind=True,
    queue="documents",
    max_retries=5,
    default_retry_delay=60
)
def process_document(
    self,
    document_id: str,
    file_path: str,
    file_type: str,
    metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Обработать документ: распарсить, разбить на чанки, векторизовать.
    
    Args:
        document_id: Идентификатор документа
        file_path: Путь к файлу
        file_type: Тип файла (pdf, txt, docx, audio)
        metadata: Дополнительные метаданные
        
    Returns:
        Результат обработки
    """
    logger.info(f"Начало обработки документа: {document_id}, тип: {file_type}")
    
    try:
        # Шаг 1: Парсинг документа (гибридный: Docling + Occular-ocr)
        from src.indexing.hybrid_parser import get_hybrid_parser
        hybrid = get_hybrid_parser()
        parsed_doc = hybrid.parse(file_path)
        
        # Структурированный вывод
        parsed_content = {
            'text': parsed_doc.full_text,
            'pages': [{'num': p.page_num, 'text': p.text} for p in parsed_doc.pages],
            'tables': sum((p.tables for p in parsed_doc.pages), []),
            'segments': [p.text for p in parsed_doc.pages],
            'metadata': parsed_doc.metadata,
            'parse_method': parsed_doc.parse_method
        }
        
        # Шаг 1.5: Авто-классификация
        from src.indexing.auto_tagger import get_auto_tagger
        tagger = get_auto_tagger()
        classification = tagger.classify(parsed_doc.full_text, parsed_doc.filename)
        
        logger.info(
            f"Документ распарсен: {parsed_doc.filename}, "
            f"метод: {parsed_doc.parse_method}, "
            f"страниц: {len(parsed_doc.pages)}, "
            f"тип: {classification.document_type.value} ({classification.confidence:.0%}), "
            f"теги: {classification.tags}"
        )
        
        # Шаг 2: Чанкинг
        chunker = DocumentChunker()
        chunks = chunker.chunk(parsed_content, file_type)
        
        logger.info(f"Документ разбит на чанки: {document_id}, количество: {len(chunks)}")
        
        # Шаг 3: Векторизация (цепочка задач)
        vectorize_document.delay(document_id, chunks, metadata or {})
        
        return {
            "document_id": document_id,
            "status": "chunked",
            "chunks_count": len(chunks),
            "parsed_segments": len(parsed_content.get("segments", []))
        }
        
    except Exception as exc:
        logger.error(f"Ошибка обработки документа {document_id}: {exc}")
        
        # Повторная попытка с экспоненциальной задержкой
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


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
