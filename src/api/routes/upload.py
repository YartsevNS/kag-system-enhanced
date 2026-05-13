"""
Маршруты для загрузки и обработки документов
"""

from typing import Optional, List
from fastapi import APIRouter, UploadFile, File, HTTPException, Form, BackgroundTasks, Depends
from loguru import logger
import uuid
from datetime import datetime

from src.models import DocumentUpload, DocumentStatus
from src.api.services.document_service import document_service
from src.security.validator import SecurityValidator, SecurityValidationError
from src.api.middleware.auth_v2 import get_current_user, get_current_user_optional
from src.database.user_models import User

router = APIRouter()


@router.post("/", response_model=DocumentStatus, summary="Загрузить документ")
async def upload_document(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
    current_user: Optional[User] = Depends(get_current_user_optional)
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
        content = await file.read()

        try:
            SecurityValidator.validate_file_upload(
                file_path="",
                filename=file.filename or "unknown",
                file_size=len(content),
                mime_type=file.content_type
            )
        except SecurityValidationError as ve:
            raise HTTPException(status_code=400, detail=ve.message)

        # Extract user info for document access control
        uploaded_by = current_user.id if current_user else None
        group_ids = [g.id for g in current_user.groups] if current_user and current_user.groups else None

        record = await document_service.upload_document(
            filename=file.filename,
            file_content=content,
            file_type=file.content_type,
            uploaded_by=uploaded_by,
            group_ids=group_ids
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
    background_tasks: BackgroundTasks = None,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Пакетная загрузка нескольких документов.

    - **files**: Список файлов для загрузки

    Возвращает список document_id для отслеживания статуса.
    """
    logger.info(f"Пакетная загрузка: {len(files)} файлов")

    uploaded_by = current_user.id if current_user else None
    group_ids = [g.id for g in current_user.groups] if current_user and current_user.groups else None

    results = []
    for file in files:
        try:
            content = await file.read()

            try:
                SecurityValidator.validate_file_upload(
                    file_path="",
                    filename=file.filename or "unknown",
                    file_size=len(content),
                    mime_type=file.content_type
                )
            except SecurityValidationError as ve:
                results.append({
                    "filename": file.filename,
                    "status": "error",
                    "error": ve.message
                })
                continue

            record = await document_service.upload_document(
                filename=file.filename,
                file_content=content,
                file_type=file.content_type,
                uploaded_by=uploaded_by,
                group_ids=group_ids
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
async def list_documents(
    limit: int = 100,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Получить список загруженных документов.

    - **limit**: Максимальное количество записей

    Для не-администраторов возвращаются только документы их групп.
    """
    documents = document_service.list_documents(limit)

    # Filter by group access for non-admin users
    if current_user and not current_user.is_admin:
        user_group_ids = [g.id for g in current_user.groups] if current_user.groups else []
        if user_group_ids:
            documents = [
                d for d in documents
                if d.group_ids is None or not d.group_ids or any(g in d.group_ids for g in user_group_ids)
            ]
        else:
            # User has no groups - only show public documents (empty group_ids)
            documents = [
                d for d in documents
                if d.group_ids is None or not d.group_ids
            ]
    
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
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "updated_at": d.updated_at.isoformat() if d.updated_at else None,
                "uploaded_by": d.uploaded_by
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


@router.post("/{document_id}/reindex", summary="Переиндексировать документ")
async def reindex_document(document_id: str):
    """
    Переиндексировать документ (заново создать вектора).
    """
    from src.indexing.embeddings_service import embeddings_service
    await embeddings_service.initialize()
    
    try:
        await document_service.process_document(document_id)
        record = document_service.get_document(document_id)
        return {
            "status": "ok",
            "document_id": document_id,
            "chunks_count": record.chunks_count if record else 0
        }
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Файл документа не найден")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{document_id}/chunks", summary="Чанки документа")
async def get_document_chunks(
    document_id: str,
    offset: int = 0,
    limit: int = 10
):
    """Получить чанки конкретного документа из Qdrant."""
    from src.indexing.qdrant_service import qdrant_service
    
    try:
        results = qdrant_service.scroll_points(
            filter={
                "must": [{"key": "document_id", "match": {"value": document_id}}]
            },
            limit=limit,
            offset=offset
        )
        
        chunks = []
        for r in results:
            payload = r.get("payload", {})
            chunks.append({
                "id": r.get("id", ""),
                "text": payload.get("text", payload.get("content", "")),
                "chunk_index": payload.get("chunk_index", 0),
                "document_id": document_id
            })
        
        return {"chunks": chunks, "total": len(chunks), "offset": offset, "limit": limit}
    except Exception as e:
        logger.error(f"Ошибка получения чанков: {e}")
        return {"chunks": [], "total": 0, "error": str(e)}


async def _process_document_async(document_id: str):
    """Фоновая обработка документа"""
    try:
        result = await document_service.process_document(document_id)
        logger.info(f"Документ обработан: {document_id}, результат: {result}")
    except Exception as e:
        logger.error(f"Ошибка обработки документа {document_id}: {e}")
