"""
Маршруты для загрузки и обработки документов
"""

from typing import Optional, List
from fastapi import APIRouter, UploadFile, File, HTTPException, Form, BackgroundTasks
from loguru import logger
import uuid
from datetime import datetime

from src.models import DocumentUpload, DocumentStatus
from src.api.services.document_service import document_service

router = APIRouter()


@router.post("/", response_model=DocumentStatus, summary="Загрузить документ")
async def upload_document(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None
):
    """
    Загрузить документ для индексации.

    Поддерживаемые форматы:
    - PDF (с OCR для таблиц и изображений)
    - TXT, MD
    - DOCX
    - CSV

    - **file**: Файл для загрузки

    Возвращает document_id для отслеживания статуса.
    """
    logger.info(f"Загрузка документа: {file.filename}, тип: {file.content_type}")

    try:
        # Читаем содержимое файла
        content = await file.read()

        # Загружаем документ (создаёт запись в _documents)
        record = await document_service.upload_document(
            filename=file.filename,
            file_content=content,
            file_type=file.content_type
        )

        # Запускаем обработку в фоне ОБЯЗАТЕЛЬНО
        if background_tasks is not None:
            logger.info(f"Планирую фоновую обработку: {record.document_id}")
            background_tasks.add_task(_process_document_async, record.document_id)
        else:
            # Если BackgroundTasks недоступен, обрабатываем синхронно
            logger.warning("BackgroundTasks недоступен, обрабатываю синхронно")
            try:
                result = await document_service.process_document(record.document_id)
                logger.info(f"Документ обработан синхронно: {record.document_id}, результат: {result}")
            except Exception as e:
                logger.error(f"Ошибка синхронной обработки: {e}")
                record.status = "failed"
                record.error = str(e)

        return DocumentStatus(
            document_id=record.document_id,
            status=record.status,
            progress=record.progress,
            error=record.error if record.status == "failed" else None,
            created_at=record.created_at,
            updated_at=record.updated_at
        )

    except Exception as e:
        logger.error(f"Ошибка загрузки документа: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/batch", summary="Пакетная загрузка документов")
async def upload_documents_batch(
    files: list[UploadFile] = File(...),
    background_tasks: BackgroundTasks = None
):
    """
    Пакетная загрузка нескольких документов.

    - **files**: Список файлов для загрузки

    Возвращает список document_id для отслеживания статуса.
    """
    logger.info(f"Пакетная загрузка: {len(files)} файлов")

    results = []
    for file in files:
        try:
            content = await file.read()
            record = await document_service.upload_document(
                filename=file.filename,
                file_content=content,
                file_type=file.content_type
            )

            # Запускаем обработку в фоне ОБЯЗАТЕЛЬНО
            if background_tasks is not None:
                background_tasks.add_task(_process_document_async, record.document_id)
            else:
                try:
                    await document_service.process_document(record.document_id)
                except Exception as e:
                    logger.error(f"Ошибка обработки {file.filename}: {e}")

            results.append({
                "document_id": record.document_id,
                "filename": file.filename,
                "status": record.status,
                "file_size": record.file_size
            })
        except Exception as e:
            logger.error(f"Ошибка загрузки {file.filename}: {e}")
            results.append({
                "filename": file.filename,
                "status": "error",
                "error": str(e)
            })

    return {"uploaded": len(results), "documents": results}


@router.get("/{document_id}/status", response_model=DocumentStatus, summary="Статус документа")
async def get_document_status(document_id: str):
    """
    Получить статус обработки документа.

    - **document_id**: Идентификатор документа

    Статусы:
    - pending: Ожидает обработки
    - processing: В процессе обработки
    - completed: Обработка завершена
    - failed: Ошибка обработки
    """
    record = document_service.get_document_status(document_id)
    
    if not record:
        raise HTTPException(status_code=404, detail="Документ не найден")

    return DocumentStatus(
        document_id=record.document_id,
        status=record.status,
        progress=record.progress,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at
    )


@router.get("/list", summary="Список документов")
async def list_documents(limit: int = 100):
    """
    Получить список загруженных документов.

    - **limit**: Максимальное количество записей
    """
    documents = document_service.list_documents(limit)
    
    return {
        "total": len(documents),
        "documents": [
            {
                "document_id": d.document_id,
                "filename": d.filename,
                "file_type": d.file_type,
                "file_size": d.file_size,
                "status": d.status,
                "progress": d.progress,
                "chunks_count": d.chunks_count,
                "created_at": d.created_at.isoformat()
            }
            for d in documents
        ]
    }


@router.delete("/{document_id}", summary="Удалить документ")
async def delete_document(document_id: str):
    """
    Удалить документ и его индексы.

    - **document_id**: Идентификатор документа
    """
    success = await document_service.delete_document(document_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="Документ не найден")

    return {"status": "ok", "document_id": document_id}


async def _process_document_async(document_id: str):
    """Фоновая обработка документа"""
    try:
        result = await document_service.process_document(document_id)
        logger.info(f"Документ обработан: {document_id}, результат: {result}")
    except Exception as e:
        logger.error(f"Ошибка обработки документа {document_id}: {e}")
