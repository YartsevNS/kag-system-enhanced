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
    from src.indexing.qdrant_service import get_qdrant_service
    qdrant_service = get_qdrant_service()
    
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


@router.get("/{document_id}/details", summary="Детальная информация о документе")
async def get_document_details(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Получить расширенную информацию о документе с тэгами, типом, пользователем."""
    from src.database.session import _get_engine, _SessionLocal
    from src.database.document_models import Document
    from src.database.user_models import User as UserModel
    from src.indexing.auto_tagger import get_auto_tagger
    
    _get_engine()
    session = _SessionLocal()
    try:
        record = session.query(Document).filter(Document.id == document_id).first()
        if not record:
            raise HTTPException(status_code=404, detail="Документ не найден")
        
        # Get user info
        uploaded_by_name = None
        if record.uploaded_by:
            user = session.query(UserModel).filter(UserModel.id == record.uploaded_by).first()
            if user:
                uploaded_by_name = user.username
        
        # Get tags from Qdrant payload or auto-tagger
        tags = []
        doc_type = "unknown"
        recognized_title = record.filename
        
        try:
            from src.indexing.qdrant_service import get_qdrant_service
            qdrant_service = get_qdrant_service()
            results = qdrant_service.scroll_points(
                filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
                limit=1
            )
            if results:
                payload = results[0].get("payload", {})
                tags = payload.get("tags", [])
                doc_type = payload.get("document_type", "unknown")
                recognized_title = payload.get("title", record.filename)
                chunks_total = payload.get("chunks_total", 0)
            else:
                chunks_total = 0
        except Exception as e:
            logger.warning(f"Не удалось получить тэги из Qdrant: {e}")
            chunks_total = 0
        
        # If no tags, try auto-tagger (but we need document text)
        if not tags:
            try:
                # Try to get first chunk text for tagging
                from src.indexing.qdrant_service import get_qdrant_service
                qdrant_service = get_qdrant_service()
                results = qdrant_service.scroll_points(
                    filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
                    limit=3
                )
                if results:
                    text = " ".join([r.get("payload", {}).get("text", "") for r in results])
                    tagger = get_auto_tagger()
                    classification = tagger.classify(text, record.filename)
                    tags = classification.tags
                    if classification.confidence > 0.3:
                        doc_type = classification.document_type.value
            except Exception as e:
                logger.warning(f"Auto-tagger failed: {e}")
        
        return {
            "document_id": record.id,
            "filename": record.filename,
            "recognized_title": recognized_title,
            "file_type": record.mime_type or "unknown",
            "file_size": record.file_size or 0,
            "status": record.status,
            "document_type": doc_type,
            "tags": tags,
            "chunks_count": chunks_total,
            "uploaded_by": record.uploaded_by,
            "uploaded_by_name": uploaded_by_name,
            "file_hash": record.file_hash,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None
        }
    finally:
        session.close()


@router.get("/{document_id}/thumbnail", summary="Миниатюра документа")
async def get_document_thumbnail(document_id: str):
    """Вернуть миниатюру документа (WebP/Png), с кэшированием."""
    from pathlib import Path
    from fastapi.responses import FileResponse, Response
    
    thumb_dir = Path("/app/data/thumbnails")
    thumb_path = thumb_dir / f"{document_id}.webp"
    
    # Если миниатюра уже есть в кэше — отдаём сразу
    if thumb_path.exists():
        return FileResponse(thumb_path, media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"})
    
    # Пробуем сгенерировать на лету
    try:
        # Ищем файл документа по всем возможным директориям
        file_path = None
        for upload_dir in [
            Path("/app/data/uploads"),
            Path("/app/user_data/uploads"),
            Path("/tmp/kag_uploads"),
        ]:
            if not upload_dir.exists():
                continue
            for f in upload_dir.iterdir():
                if f.is_file() and f.name.startswith(document_id):
                    file_path = f
                    break
            if file_path:
                break
        
        if file_path and file_path.exists():
            # Генерируем через document_service
            thumb = document_service._generate_thumbnail(document_id, file_path)
            if thumb and thumb.exists():
                return FileResponse(thumb, media_type="image/webp",
                    headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        logger.warning(f"On-the-fly thumbnail failed for {document_id}: {e}")
    
    # Placeholder SVG
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="280" viewBox="0 0 400 280">
      <rect width="400" height="280" fill="#0f1011" rx="12"/>
      <rect width="400" height="280" fill="#5e6ad2" opacity="0.06" rx="12"/>
      <text x="200" y="130" text-anchor="middle" fill="#62666d" font-family="Inter,sans-serif" font-size="40">📄</text>
      <text x="200" y="170" text-anchor="middle" fill="#8a8f98" font-family="Inter,sans-serif" font-size="14">Миниатюра недоступна</text>
    </svg>'''
    return Response(content=svg.encode(), media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"})


@router.get("/{document_id}/preview", summary="Файл документа для просмотра")
async def get_document_preview(document_id: str):
    """Вернуть файл документа для inline-просмотра в браузере."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    
    # Ищем файл по всем возможным директориям
    for upload_dir in [Path("/app/data/uploads"), Path("/app/user_data/uploads"), Path("/tmp/kag_uploads")]:
        if not upload_dir.exists():
            continue
        for f in upload_dir.iterdir():
            if f.is_file() and f.name.startswith(document_id):
                mime = "application/pdf" if f.suffix.lower() == '.pdf' else "application/octet-stream"
                return FileResponse(f, media_type=mime,
                    headers={"Content-Disposition": "inline", "Cache-Control": "public, max-age=3600"})
    
    raise HTTPException(status_code=404, detail="Файл не найден")


async def _process_document_async(document_id: str):
    """Фоновая обработка документа"""
    try:
        result = await document_service.process_document(document_id)
        logger.info(f"Документ обработан: {document_id}, результат: {result}")
    except Exception as e:
        logger.error(f"Ошибка обработки документа {document_id}: {e}")
