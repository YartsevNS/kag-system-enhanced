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

        # Запускаем обработку строго через семафор
        asyncio.create_task(_process_document_async(record.document_id))

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

            # Запускаем обработку строго через семафор
            asyncio.create_task(_process_document_async(record.document_id))

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
    
    # Enrich with config_store metadata (document_type, title, etc.)
    enriched = []
    for d in documents:
        item = {
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
        # Merge config_store metadata
        try:
            from src.api.services.config_store import config_store
            meta = config_store.get("documents", d.document_id)
            if meta:
                item["document_type"] = meta.get("document_type", "")
                item["recognized_title"] = meta.get("recognized_title", "")
                item["summary"] = meta.get("summary", "")
                item["topics"] = meta.get("topics", [])
        except Exception:
            pass
        enriched.append(item)
    
    return {
        "total": len(enriched),
        "documents": enriched
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
        # Получаем ВСЕ чанки документа (Qdrant scroll возвращает неупорядоченно)
        all_results = qdrant_service.scroll_points(
            filter={
                "must": [{"key": "document_id", "match": {"value": document_id}}]
            },
            limit=10000
        )
        
        all_chunks = []
        for r in all_results:
            payload = r.get("payload", {})
            all_chunks.append({
                "id": r.get("id", ""),
                "chunk_id": payload.get("chunk_id", ""),
                "text": payload.get("text", payload.get("content", "")),
                "chunk_index": payload.get("chunk_index", 0),
                "chunk_seq": payload.get("chunk_seq", payload.get("chunk_index", 0)),
                "metadata": payload.get("metadata", {}),
                "document_id": document_id
            })
        
        # Сортируем: chunk_seq если есть, иначе номер из chunk_id
        def sort_key(c):
            seq = c.get("chunk_seq")
            if seq and seq > 0:
                return seq
            cid = c.get("chunk_id", "")
            try:
                return int(cid.replace("chunk_", "").split("_")[0])
            except (ValueError, IndexError):
                return c.get("chunk_index", 0)
        all_chunks.sort(key=sort_key)
        
        total = len(all_chunks)
        chunks = all_chunks[offset:offset + limit]
        
        return {"chunks": chunks, "total": total, "offset": offset, "limit": limit}
    except Exception as e:
        logger.error(f"Ошибка получения чанков: {e}")
        return {"chunks": [], "total": 0, "error": str(e)}


@router.get("/{document_id}/details", summary="Детальная информация о документе")
async def get_document_details(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Получить расширенную информацию о документе с тэгами, типом, пользователем."""
    from src.api.services.config_store import config_store
    from src.indexing.auto_tagger import get_auto_tagger

    # Первичный источник — document_service (in-memory, всегда актуальный)
    record = document_service.get_document_status(document_id)
    
    # Получаем запись из config_store через get_all (надёжнее чем get по ID)
    record_data = None
    try:
        all_docs = config_store.get_all("documents")
        record_data = all_docs.get(document_id)
    except Exception:
        record_data = config_store.get("documents", document_id)
    
    if not record and not record_data:
        raise HTTPException(status_code=404, detail="Документ не найден")

    # Приоритет: in-memory record > config_store
    filename = record.filename if record else record_data.get("filename", "unknown")
    file_type = record.file_type if record else record_data.get("file_type", "unknown")
    file_size = record.file_size if record else record_data.get("file_size", 0)
    file_hash = record_data.get("file_hash", "") if record_data else ""
    status = record.status if record else record_data.get("status", "unknown")
    uploaded_by = record.uploaded_by if record else record_data.get("uploaded_by")
    created_at_raw = record.created_at if record else record_data.get("created_at")
    updated_at_raw = record.updated_at if record else record_data.get("updated_at")
    
    # chunks_count: приоритет in-memory, затем config_store, затем 0
    chunks_count = record.chunks_count if record else record_data.get("chunks_count", 0) if record_data else 0
    
    # Если in-memory показывает 0, но в Qdrant есть чанки — обновим
    if chunks_count == 0:
        try:
            from src.indexing.qdrant_service import get_qdrant_service
            qdrant = get_qdrant_service()
            qdrant_results = qdrant.scroll_points(
                filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
                limit=1
            )
            # Считаем реальное количество через отдельный запрос
            all_qdrant = qdrant.scroll_points(
                filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
                limit=100000
            )
            real_count = len(all_qdrant)
            if real_count > 0:
                chunks_count = real_count
                # Обновим в памяти и в БД
                if record:
                    record.chunks_count = real_count
                    document_service._save_document_to_db(document_id)
        except Exception:
            pass
    
    created_at = created_at_raw.isoformat() if hasattr(created_at_raw, 'isoformat') else str(created_at_raw) if created_at_raw else None
    updated_at = updated_at_raw.isoformat() if hasattr(updated_at_raw, 'isoformat') else str(updated_at_raw) if updated_at_raw else None
    
    # document_type и recognized_title: приоритет in-memory, затем config_store
    doc_type_inmem = getattr(record, 'document_type', None) if record else None
    doc_title_inmem = getattr(record, 'recognized_title', None) if record else None
    cfg_document_type = doc_type_inmem or (record_data.get('document_type') if record_data else None)
    cfg_recognized_title = doc_title_inmem or (record_data.get('recognized_title') if record_data else None)

    # Get user info
    uploaded_by_name = None
    if uploaded_by:
        try:
            from src.database.session import _get_engine, _SessionLocal
            from src.database.user_models import User as UserModel
            _get_engine()
            session = _SessionLocal()
            user = session.query(UserModel).filter(UserModel.id == uploaded_by).first()
            if user:
                uploaded_by_name = user.username
            session.close()
        except Exception:
            pass

    # Get tags from Qdrant payload or auto-tagger
    tags = []
    doc_type = "unknown"
    recognized_title = filename
    chunks_total = 0

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
            recognized_title = payload.get("title", filename)
            chunks_total = payload.get("chunks_total", 0)
    except Exception as e:
        logger.warning(f"Не удалось получить тэги из Qdrant: {e}")

    # If no tags, try auto-tagger (but we need document text)
    if not tags:
        try:
            from src.indexing.qdrant_service import get_qdrant_service
            qdrant_service = get_qdrant_service()
            results = qdrant_service.scroll_points(
                filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
                limit=3
            )
            if results:
                text = " ".join([r.get("payload", {}).get("text", "") for r in results])
                tagger = get_auto_tagger()
                classification = tagger.classify(text, filename)
                tags = classification.tags
                if classification.confidence > 0.3:
                    doc_type = classification.document_type.value
        except Exception as e:
            logger.warning(f"Auto-tagger failed: {e}")

    return {
        "document_id": document_id,
        "filename": filename,
        "recognized_title": cfg_recognized_title or recognized_title or filename,
        "file_type": file_type,
        "file_size": file_size or 0,
        "status": status,
        "document_type": cfg_document_type or doc_type or "unknown",
        "tags": tags,
        "chunks_count": chunks_count,
        "uploaded_by": uploaded_by,
        "uploaded_by_name": uploaded_by_name,
        "file_hash": file_hash,
        "created_at": created_at,
        "updated_at": updated_at
    }


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


@router.post("/reanalyze-all", summary="Переанализировать все документы")
async def reanalyze_all_documents():
    """
    Фоновый переанализ всех completed-документов через LLM.
    Определяет document_type, recognized_title, summary, topics.
    """
    import asyncio
    try:
        from src.api.services.document_analyzer import document_analyzer
        from src.api.services.config_store import config_store
        from src.api.services.document_service import document_service
        
        all_docs = config_store.get_all("documents") or {}
        to_analyze = []
        for did, doc in all_docs.items():
            if not isinstance(doc, dict) or doc.get("status") != "completed":
                continue
            # Skip if already has a proper type
            dt = doc.get("document_type", "")
            if dt and dt not in ("unknown", "other", ""):
                continue
            to_analyze.append((did, doc))
        
        async def analyze_one(did, doc):
            try:
                # Get first chunk text
                from src.indexing.embeddings_service import embeddings_service
                if embeddings_service._qdrant_client is None:
                    await embeddings_service.initialize()
                chunks = await embeddings_service.get_document_chunks(did)
                first_text = chunks[0].get("content", "") if chunks else ""
                if first_text:
                    await document_analyzer.analyze_and_save(did, first_text, doc.get("filename", ""))
                    return {"id": did, "status": "ok"}
                return {"id": did, "status": "no_chunks"}
            except Exception as e:
                return {"id": did, "status": "error", "error": str(e)}
        
        results = []
        for did, doc in to_analyze:
            r = await analyze_one(did, doc)
            results.append(r)
        
        return {"status": "ok", "total": len(to_analyze), "results": results}
    except Exception as e:
        return {"status": "error", "message": str(e)}


import asyncio
# Семафор: только 1 документ обрабатывается одновременно
_process_sem = asyncio.Semaphore(1)

async def _process_document_async(document_id: str):
    """Фоновая обработка документа (строго по одному, без новых event loop'ов)"""
    logger.info(f"⏳ Ожидание семафора для {document_id}...")
    async with _process_sem:
        logger.info(f"▶️ Семафор получен, начинаю обработку {document_id}")
        try:
            result = await document_service.process_document(document_id)
            logger.info(f"Документ обработан: {document_id}, результат: {result}")
        except Exception as e:
            logger.error(f"Ошибка обработки документа {document_id}: {e}")


# ============================================================
# Версионность и контроль дубликатов
# ============================================================

@router.get("/{document_id}/versions", summary="История версий документа")
async def get_document_versions(document_id: str):
    """
    Получить информацию о версиях документа.
    
    Возвращает текущую версию, хеш, хеш предыдущей версии,
    и флаг has_previous указывающий, есть ли с чем сравнивать.
    """
    try:
        from src.api.services.document_service import document_service
        from src.api.services.config_store import config_store

        # Получаем из кэша или БД
        record = document_service._documents.get(document_id)
        if not record:
            meta = config_store.get("documents", document_id)
            if not meta:
                raise HTTPException(status_code=404, detail="Документ не найден")
            return {
                "document_id": document_id,
                "version": int(meta.get("version", 1)),
                "file_hash": meta.get("file_hash", ""),
                "previous_hash": meta.get("previous_hash", ""),
                "has_previous": bool(meta.get("previous_hash")),
                "has_original_text": bool(meta.get("original_text")),
            }

        return {
            "document_id": document_id,
            "version": record.version,
            "file_hash": record.file_hash,
            "previous_hash": record.previous_hash,
            "has_previous": bool(record.previous_hash),
            "has_original_text": bool(record.original_text),
        }
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}


@router.get("/{document_id}/diff", summary="Сравнение версий документа")
async def diff_document_versions(document_id: str):
    """
    Сравнить текущую версию документа с предыдущей.
    
    Возвращает diff: что изменилось в тексте между версиями.
    Полезно при повторной загрузке обновлённого документа.
    """
    try:
        from src.api.services.document_service import document_service
        result = document_service.compare_versions(document_id)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}


@router.get("/check-duplicate", summary="Проверить файл на дубликат")
async def check_duplicate(hash: str = ""):
    """
    Проверить, существует ли уже документ с таким SHA-256 хешем.
    
    Используется на фронтенде перед загрузкой: если хеш совпадает,
    показываем предупреждение «Этот документ уже загружен».
    
    Args:
        hash: SHA-256 хеш файла (64 символа hex)
    """
    if not hash or len(hash) != 64:
        return {"duplicate": False, "message": "Невалидный хеш"}
    try:
        from src.api.services.document_service import document_service
        existing = document_service._find_by_hash(hash)
        if existing:
            return {
                "duplicate": True,
                "document_id": existing.document_id,
                "filename": existing.filename,
                "version": existing.version,
                "status": existing.status,
                "message": f"Документ уже загружен: {existing.filename} (v{existing.version})"
            }
        return {"duplicate": False, "message": "Документ не найден — можно загружать"}
    except Exception as e:
        return {"duplicate": False, "error": str(e)}


@router.post("/reprocess-pending", summary="Перезапустить обработку pending-документов")
async def reprocess_pending_documents():
    """
    Найти все документы со статусом 'pending' и запустить их обработку заново.
    Полезно после падения контейнера (OOM) — pending-документы остались без обработки.
    """
    import asyncio
    from src.api.services.config_store import config_store
    
    docs = config_store.get_all("documents") or {}
    pending = [(did, doc) for did, doc in docs.items() 
               if isinstance(doc, dict) and doc.get('status') == 'pending']
    
    if not pending:
        return {"success": True, "message": "Нет pending-документов", "count": 0}
    
    logger.info(f"Запускаю переобработку {len(pending)} pending-документов")
    count = 0
    for did, doc in pending:
        try:
            asyncio.create_task(_process_document_async(did))
            count += 1
        except Exception as e:
            logger.warning(f"Не удалось запустить {did}: {e}")
    
    return {"success": True, "message": f"Запущена переобработка", "count": count}
