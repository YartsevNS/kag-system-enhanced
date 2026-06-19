"""
Маршруты для загрузки и обработки документов

Архитектура (как Paperless-ngx):
1. Клиент шлёт POST /api/v1/upload/ с multipart/form-data
"""

import time
from typing import Dict, Any, Callable

# ── In-memory кеш для снижения нагрузки на БД ────────────────────────
_cache: Dict[str, Dict[str, Any]] = {}  # key - {data, expires_at}

def _cached(ttl: float = 2.0):
    """Декоратор: кеширует результат функции на ttl секунд (в памяти)."""
    def decorator(func: Callable) -> Callable:
        async def wrapper(*args, **kwargs):
            # Ключ кеша: имя функции + args + kwargs
            cache_key = f"{func.__name__}:{str(args)}:{str(sorted(kwargs.items()))}"
            now = time.monotonic()
            cached = _cache.get(cache_key)
            if cached and cached["expires_at"] > now:
                return cached["data"]
            result = await func(*args, **kwargs)
            _cache[cache_key] = {"data": result, "expires_at": now + ttl}
            # Очистка старых записей (каждые 100 вставок)
            if len(_cache) > 100:
                for k in list(_cache.keys()):
                    if _cache[k]["expires_at"] < now:
                        del _cache[k]
            return result
        return wrapper
    return decorator

Архитектура (как Paperless-ngx):
Обработка асинхронная, не блокирует upload.
"""
import os
import io
import uuid
import asyncio
import json
from pathlib import Path
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Request, Response
from loguru import logger

from src.models import DocumentStatus
from src.api.services.document_service import document_service
from src.security.validator import SecurityValidator, SecurityValidationError
from src.api.middleware.auth_v2 import get_current_user_optional
from src.database.user_models import User

# Celery задача обработки документов (вместо asyncio.Queue)
from src.indexing.tasks import process_document as celery_process_document

router = APIRouter()

# Лимит одного файла (1GB)
MAX_FILE_SIZE = 1024 * 1024 * 1024

# Rate limiter: не более N запросов в минуту на upload
import time
from collections import defaultdict
_RATE_STORE: dict = defaultdict(list)
_RATE_LIMIT = 10       # запросов
_RATE_WINDOW = 60      # секунд


def _check_rate_limit(ip: str):
    """Проверить лимит upload-запросов. 429 при превышении."""
    now = time.time()
    window_start = now - _RATE_WINDOW
    _RATE_STORE[ip] = [t for t in _RATE_STORE[ip] if t > window_start]
    if len(_RATE_STORE[ip]) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail={
            "code": "RATE_LIMIT",
            "message": f"Слишком много запросов. Максимум {_RATE_LIMIT} в минуту.",
        })
    _RATE_STORE[ip].append(now)

# Директория для TUS чанков (временные файлы)
TUS_DIR = Path("/tmp/tus_uploads")


# ============================================================
# TUS - resumable upload protocol (RFC-описание)
# Позволяет загружать файлы до 1GB по частям.
# ============================================================

TUS_DIR.mkdir(parents=True, exist_ok=True)


def _tus_meta_path(upload_id: str) -> Path:
    """Путь к файлу метаданных TUS-сессии."""
    return TUS_DIR / f"{upload_id}.meta"


def _tus_file_path(upload_id: str) -> Path:
    """Путь к файлу с частично загруженными данными."""
    return TUS_DIR / f"{upload_id}.bin"


@router.options("/tus")
async def tus_options():
    """TUS: вернуть поддерживаемые опции протокола."""
    return Response(
        headers={
            "Tus-Resumable": "1.0.0",
            "Tus-Version": "1.0.0",
            "Tus-Extension": "creation,termination",
            "Tus-Max-Size": str(MAX_FILE_SIZE),
        }
    )


@router.post("/tus", status_code=201)
async def tus_create(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    TUS: создать новую сессию загрузки.

    Заголовки запроса:
    - Upload-Length: общий размер файла в байтах
    - Upload-Metadata: base64(filename, content_type)
    """
    # Проверка протокола
    if request.headers.get("Tus-Resumable") != "1.0.0":
        raise HTTPException(status_code=412, detail="Tus-Resumable: 1.0.0 required")

    upload_length = request.headers.get("Upload-Length")
    if not upload_length:
        raise HTTPException(status_code=400, detail="Upload-Length header required")

    try:
        total_size = int(upload_length)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Upload-Length")

    if total_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"File too large: {total_size} > {MAX_FILE_SIZE}")

    # Парсим метаданные (Upload-Metadata: filename base64..., content_type base64...)
    filename = f"file_{uuid.uuid4().hex[:8]}"
    content_type = "application/octet-stream"
    metadata_header = request.headers.get("Upload-Metadata", "")
    for part in metadata_header.split(","):
        part = part.strip()
        if not part:
            continue
        if " " in part:
            key, b64_val = part.split(" ", 1)
            try:
                import base64
                decoded = base64.b64decode(b64_val).decode("utf-8")
                if key == "filename":
                    filename = decoded
                elif key == "content_type":
                    content_type = decoded
            except Exception:
                pass

    upload_id = str(uuid.uuid4())

    # Сохраняем метаданные сессии
    meta = {
        "upload_id": upload_id,
        "filename": filename,
        "content_type": content_type,
        "total_size": total_size,
        "offset": 0,
        "created_at": datetime.utcnow().isoformat(),
        "uploaded_by": str(current_user.id) if current_user else None,
    }
    with open(_tus_meta_path(upload_id), "w") as f:
        json.dump(meta, f)

    # Создаём пустой файл для чанков (pre-allocate не обязателен)
    _tus_file_path(upload_id).touch()

    logger.info(f"[TUS] Сессия создана: {upload_id}, файл: {filename}, размер: {total_size}")

    # Location - URL для последующих PATCH/HEAD запросов
    return Response(
        status_code=201,
        headers={
            "Location": f"/api/v1/upload/tus/{upload_id}",
            "Tus-Resumable": "1.0.0",
            "Upload-Offset": "0",
        },
    )


@router.head("/tus/{upload_id}")
async def tus_head(upload_id: str):
    """TUS: получить текущий статус загрузки (сколько байт уже получено)."""
    meta_path = _tus_meta_path(upload_id)
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")

    with open(meta_path) as f:
        meta = json.load(f)

    file_path = _tus_file_path(upload_id)
    offset = file_path.stat().st_size if file_path.exists() else 0

    return Response(
        headers={
            "Upload-Offset": str(offset),
            "Upload-Length": str(meta["total_size"]),
            "Tus-Resumable": "1.0.0",
        }
    )


@router.patch("/tus/{upload_id}", status_code=204)
async def tus_patch(
    upload_id: str,
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    TUS: загрузить очередной чанк.

    Заголовки:
    - Upload-Offset: текущая позиция (должна совпадать с размером файла на диске)
    - Content-Type: application/offset+octet-stream
    """
    if request.headers.get("Tus-Resumable") != "1.0.0":
        raise HTTPException(status_code=412, detail="Tus-Resumable: 1.0.0 required")

    meta_path = _tus_meta_path(upload_id)
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Upload session not found")

    with open(meta_path) as f:
        meta = json.load(f)

    file_path = _tus_file_path(upload_id)
    current_offset = file_path.stat().st_size if file_path.exists() else 0

    # Проверка Upload-Offset из заголовка
    try:
        header_offset = int(request.headers.get("Upload-Offset", "0"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Upload-Offset")

    if header_offset != current_offset:
        raise HTTPException(
            status_code=409,
            detail=f"Conflict: header offset {header_offset} != actual {current_offset}",
        )

    # Читаем чанк из тела запроса и дописываем в файл
    chunk = await request.body()
    if not chunk:
        raise HTTPException(status_code=400, detail="Empty chunk")

    with open(file_path, "ab") as f:
        f.write(chunk)

    new_offset = file_path.stat().st_size
    logger.debug(f"[TUS] Чанк получен: {upload_id}, offset: {current_offset}-{new_offset}")

    # Если всё загружено - завершаем сессию
    if new_offset >= meta["total_size"]:
        logger.info(f"[TUS] Загрузка завершена: {upload_id}, файл: {meta['filename']}")

        # Собираем файл и отправляем в document_service
        try:
            with open(file_path, "rb") as f:
                file_content = f.read()

            uploaded_by = current_user.id if current_user else meta.get("uploaded_by")
            group_ids = [g.id for g in current_user.groups] if current_user and current_user.groups else None

            # Валидация
            SecurityValidator.validate_file_upload(
                file_path="",
                filename=meta["filename"],
                file_size=len(file_content),
                mime_type=meta["content_type"],
            )

            record = await document_service.upload_document(
                filename=meta["filename"],
                file_content=file_content,
                file_type=meta["content_type"],
                uploaded_by=uploaded_by,
                group_ids=group_ids,
                upload_id=upload_id,
            )

            # В очередь Celery (retry, изоляция, Redis)
            celery_process_document.delay(document_id=record.document_id)
            logger.info(f"[TUS] Документ в Celery: {record.document_id}")

        except Exception as e:
            logger.error(f"[TUS] Ошибка финализации {upload_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            # Очищаем временные файлы
            _cleanup_tus(upload_id)

    return Response(
        headers={
            "Upload-Offset": str(new_offset),
            "Tus-Resumable": "1.0.0",
        }
    )


@router.delete("/tus/{upload_id}", status_code=204)
async def tus_delete(upload_id: str):
    """TUS: отменить и удалить сессию загрузки."""
    _cleanup_tus(upload_id)
    return Response()


def _cleanup_tus(upload_id: str):
    """Удалить временные файлы TUS-сессии."""
    for p in [_tus_meta_path(upload_id), _tus_file_path(upload_id)]:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


# ============================================================
# Simple multipart upload (без TUS)
# ============================================================


@router.post("/", response_model=DocumentStatus, summary="Загрузить документ")
async def upload_document(
    file: UploadFile = File(...),
    request: Request = None,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Загрузить документ.

    Простая логика: прочитать файл, проверить, сохранить, ответить.
    Обработка (OCR, чанкинг, векторизация) - асинхронно через очередь.
    """
    upload_id = str(uuid.uuid4())
    filename = file.filename or f"unnamed_{upload_id[:8]}"

    # Rate limit
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    logger.info(f"[{upload_id}] 📥 Загрузка: {filename}, тип: {file.content_type}")

    # Читаем файл целиком
    try:
        content = await file.read()
    except Exception as e:
        logger.error(f"[{upload_id}] ❌ Ошибка чтения файла: {e}")
        raise HTTPException(status_code=400, detail={
            "code": "UPLOAD_ERROR",
            "message": f"Ошибка чтения файла: {e}",
            "upload_id": upload_id
        })

    file_size = len(content)

    # Проверка лимита размера
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail={
            "code": "VALIDATION_ERROR",
            "message": f"Файл слишком большой: {file_size} байт (макс. {MAX_FILE_SIZE} байт)",
            "upload_id": upload_id
        })

    # Валидация безопасности
    try:
        SecurityValidator.validate_file_upload(
            file_path="",
            filename=filename,
            file_size=file_size,
            mime_type=file.content_type
        )
    except SecurityValidationError as ve:
        raise HTTPException(status_code=400, detail={
            "code": "VALIDATION_ERROR",
            "message": ve.message,
            "upload_id": upload_id
        })

    # Сохраняем через document_service
    try:
        uploaded_by = current_user.id if current_user else None
        group_ids = [g.id for g in current_user.groups] if current_user and current_user.groups else None

        record = await document_service.upload_document(
            filename=filename,
            file_content=content,
            file_type=file.content_type,
            uploaded_by=uploaded_by,
            group_ids=group_ids,
            upload_id=upload_id
        )

        # В очередь Celery
        celery_process_document.delay(document_id=record.document_id)
        logger.info(f"[{upload_id}] 📋 В Celery")

        return DocumentStatus(
            document_id=record.document_id,
            status=record.status,
            progress=record.progress,
            upload_id=upload_id,
            created_at=record.created_at,
            updated_at=record.updated_at
        )

    except Exception as e:
        logger.error(f"[{upload_id}] ❌ Ошибка сохранения: {e}")
        raise HTTPException(status_code=500, detail={
            "code": "UPLOAD_ERROR",
            "message": str(e),
            "upload_id": upload_id
        })


@router.post("/batch", summary="Пакетная загрузка документов")
async def upload_documents_batch(
    files: list[UploadFile] = File(...),
    request: Request = None,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Пакетная загрузка нескольких документов.

    Каждый файл: прочитать - проверить - сохранить - в очередь.
    Если один файл упал - остальные продолжают.
    """
    # Rate limit (batch считается как 1 запрос)
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    logger.info(f"Пакетная загрузка: {len(files)} файлов")

    uploaded_by = current_user.id if current_user else None
    group_ids = [g.id for g in current_user.groups] if current_user and current_user.groups else None

    results = []
    for file in files:
        upload_id = str(uuid.uuid4())
        filename = file.filename or f"unnamed_{upload_id[:8]}"

        try:
            content = await file.read()
            file_size = len(content)

            if file_size > MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail="Файл слишком большой")

            SecurityValidator.validate_file_upload(
                file_path="",
                filename=filename,
                file_size=file_size,
                mime_type=file.content_type
            )

            record = await document_service.upload_document(
                filename=filename,
                file_content=content,
                file_type=file.content_type,
                uploaded_by=uploaded_by,
                group_ids=group_ids,
                upload_id=upload_id
            )

            celery_process_document.delay(document_id=record.document_id)

            results.append({
                "document_id": record.document_id,
                "filename": filename,
                "status": record.status,
                "file_size": record.file_size
            })

        except SecurityValidationError as ve:
            results.append({
                "filename": filename,
                "status": "error",
                "error": ve.message
            })
        except HTTPException as e:
            results.append({
                "filename": filename,
                "status": "error",
                "error": e.detail
            })
        except Exception as e:
            logger.error(f"[{upload_id}] Ошибка загрузки {filename}: {e}")
            results.append({
                "filename": filename,
                "status": "error",
                "error": str(e)
            })

    return {"uploaded": len(results), "documents": results}


@router.post("/bulk", summary="Пакетная загрузка архива (zip/tar)")
async def upload_bulk(
    file: UploadFile = File(...),
    request: Request = None,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Загрузить ZIP/TAR/GZ архив с документами.
    
    Архив распаковывается во временную папку, каждый файл внутри
    проходит валидацию и отправляется в Celery на обработку.
    
    Поддерживаемые форматы внутри архива: PDF, DOCX, TXT, MD, CSV.
    Вложенные папки игнорируются (все файлы извлекаются плоским списком).
    
    Args:
        file: ZIP/TAR/GZ файл с документами
        
    Returns:
        Список результатов загрузки каждого файла из архива
    """
    # Rate limit
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    upload_id = str(uuid.uuid4())
    filename = file.filename or f"archive_{upload_id[:8]}"
    logger.info(f"[{upload_id}] 📦 Загрузка архива: {filename}")

    # Читаем архив в память
    try:
        archive_bytes = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка чтения архива: {e}")

    # Создаём временную папку для распаковки
    extract_dir = Path(f"/tmp/bulk_{upload_id}")
    extract_dir.mkdir(parents=True, exist_ok=True)

    uploaded_by = current_user.id if current_user else None
    group_ids = [g.id for g in current_user.groups] if current_user and current_user.groups else None
    results = []
    allowed_exts = {".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".xlsx", ".xls", ".png", ".jpg", ".jpeg"}

    try:
        # Определяем тип архива по расширению
        ext = Path(filename).suffix.lower()

        if ext in (".zip",):
            import zipfile
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                for info in zf.infolist():
                    if info.filename.startswith("__") or info.filename.startswith("."):
                        continue
                    entry_ext = Path(info.filename).suffix.lower()
                    if entry_ext not in allowed_exts:
                        continue
                    # Извлекаем во временную папку
                    zf.extract(info, extract_dir)
                    extracted_path = extract_dir / info.filename
                    if not extracted_path.is_file():
                        continue

                    # Загружаем каждый файл
                    _process_bulk_file(
                        extracted_path, entry_ext, uploaded_by, group_ids,
                        upload_id, results
                    )

        elif ext in (".tar", ".gz", ".tgz"):
            import tarfile
                    # tarfile.open режим определяется по расширению
            mode = "r:gz" if ext in (".gz", ".tgz") else "r:"
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode=mode) as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    entry_ext = Path(member.name).suffix.lower()
                    if entry_ext not in allowed_exts:
                        continue
                    tf.extract(member, extract_dir)
                    extracted_path = extract_dir / member.name
                    if not extracted_path.is_file():
                        continue

                    _process_bulk_file(
                        extracted_path, entry_ext, uploaded_by, group_ids,
                        upload_id, results
                    )
        else:
            raise HTTPException(status_code=400, detail=f"Неподдерживаемый формат архива: {ext}")

    except Exception as e:
        logger.error(f"[{upload_id}] Ошибка распаковки архива: {e}")
        raise HTTPException(status_code=400, detail=f"Ошибка распаковки: {e}")
    finally:
        # Очищаем временную папку
        import shutil
        shutil.rmtree(extract_dir, ignore_errors=True)

    logger.info(f"[{upload_id}] 📦 Архив обработан: {len(results)} файлов, "
                f"ошибок: {sum(1 for r in results if r['status']=='error')}")
    return {"upload_id": upload_id, "total": len(results), "documents": results}


def _process_bulk_file(
    file_path: Path,
    file_ext: str,
    uploaded_by, group_ids, upload_id, results: list
):
    """Загрузить один файл из архива в document_service."""
    import uuid as _uuid
    try:
        with open(file_path, "rb") as f:
            content = f.read()

        if len(content) == 0:
            results.append({"filename": file_path.name, "status": "error", "error": "Empty file"})
            return
        if len(content) > MAX_FILE_SIZE:
            results.append({"filename": file_path.name, "status": "error", "error": "File too large"})
            return

        # Валидация
        SecurityValidator.validate_file_upload(
            file_path="", filename=file_path.name,
            file_size=len(content), mime_type=file_ext,
        )

        doc_id = str(_uuid.uuid4())
        # Используем document_service напрямую
        import asyncio
        record = asyncio.run(document_service.upload_document(
            filename=file_path.name, file_content=content,
            file_type=file_ext, uploaded_by=uploaded_by,
            group_ids=group_ids, upload_id=upload_id,
        ))

        # В очередь Celery
        celery_process_document.delay(document_id=record.document_id)

        results.append({
            "document_id": record.document_id,
            "filename": file_path.name,
            "status": record.status,
            "file_size": record.file_size,
        })

    except Exception as e:
        logger.warning(f"[{upload_id}] Ошибка файла {file_path.name}: {e}")
        results.append({"filename": file_path.name, "status": "error", "error": str(e)})


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


@_cached(ttl=2.0)
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
        record = document_service.get_document_status(document_id)
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


@_cached(ttl=10.0)
@router.get("/{document_id}/details", summary="Детальная информация о документе")
async def get_document_details(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Получить расширенную информацию о документе с тэгами, типом, пользователем."""
    from src.api.services.config_store import config_store
    from src.indexing.auto_tagger import get_auto_tagger

    # Первичный источник - document_service (in-memory, всегда актуальный)
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
    
    # Если in-memory показывает 0, но в Qdrant есть чанки - обновим
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
    
    # Если миниатюра уже есть в кэше - отдаём сразу
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


# ============================================================
# Версионность и контроль дубликатов
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
    показываем предупреждение Этот документ уже загружен.
    
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
        return {"duplicate": False, "message": "Документ не найден - можно загружать"}
    except Exception as e:
        return {"duplicate": False, "error": str(e)}


@router.post("/reprocess-pending", summary="Перезапустить обработку pending-документов")
async def reprocess_pending_documents():
    """
    Найти все документы со статусом 'pending' и запустить их обработку заново.
    Полезно после падения контейнера (OOM) - pending-документы остались без обработки.
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


@router.get("/queue", summary="Статус очереди обработки")
async def queue_status():
    """
    Мониторинг очереди Celery: длина, активные задачи, воркеры.
    
    Returns:
        Статус очереди обработки документов
    """
    try:
        from src.indexing.celery_app import celery_app
        
        # Получаем инспекцию воркеров
        i = celery_app.control.inspect()
        active_tasks = i.active() or {}
        reserved_tasks = i.reserved() or {}
        scheduled_tasks = i.scheduled() or {}
        
        workers = []
        total_active = 0
        total_reserved = 0
        
        for worker_name, tasks in active_tasks.items():
            workers.append({
                "name": worker_name,
                "active": len(tasks),
                "reserved": len(reserved_tasks.get(worker_name, [])),
                "scheduled": len(scheduled_tasks.get(worker_name, [])),
            })
            total_active += len(tasks)
            total_reserved += len(reserved_tasks.get(worker_name, []))
        
        return {
            "workers": workers,
            "total_workers": len(workers),
            "total_active": total_active,
            "total_reserved": total_reserved,
            "queue_depth": total_active + total_reserved,
            "status": "ok" if workers else "no_workers",
        }
        
    except Exception as e:
        logger.error(f"Ошибка получения статуса очереди: {e}")
        return {
            "status": "error",
            "error": str(e),
        }


async def _process_document_async(document_id: str):
    """Запустить фоновую обработку документа через Celery."""
    try:
        from src.indexing.tasks import process_document
        process_document.delay(document_id)
    except Exception as e:
        from loguru import logger
        logger.warning(f"Не удалось запустить Celery задачу для {document_id}: {e}")

@router.get("/{document_id}/ocr", summary="Проверить наличие OCR/Markdown")
async def check_ocr(document_id: str):
    """Проверяет, есть ли распознанный Markdown-файл для документа."""
    from src.api.services.document_service import document_service
    try:
        record = document_service.get_document_status(document_id)
        if not record:
            raise HTTPException(status_code=404, detail="Документ не найден")
        md_path = document_service._ocr_dir / f"{record.filename}.md"
        exists = md_path.exists()
        return {
            "document_id": document_id,
            "filename": record.filename,
            "ocr_md_exists": exists,
            "ocr_md_path": str(md_path) if exists else None,
            "ocr_md_size": md_path.stat().st_size if exists else 0,
        }
    except HTTPException:
        raise
    except Exception as e:
        return {"document_id": document_id, "ocr_md_exists": False, "error": str(e)}


@router.get("/{document_id}/ocr/view", summary="Просмотр распознанного Markdown")
async def view_ocr_markdown(document_id: str):
    """Возвращает содержимое распознанного Markdown-файла."""
    from src.api.services.document_service import document_service
    from fastapi.responses import PlainTextResponse
    record = document_service.get_document_status(document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Документ не найден")
    md_path = document_service._ocr_dir / f"{record.filename}.md"
    if not md_path.exists():
        raise HTTPException(status_code=404, detail="Markdown не найден")
    return PlainTextResponse(md_path.read_text(encoding="utf-8"), media_type="text/markdown")



@router.post("/{document_id}/reprocess-ocr", summary="Пересоздать OCR/Markdown для документа")
async def reprocess_ocr(document_id: str):
    """Принудительно перезапускает OCR и создание Markdown для документа."""
    from src.api.services.document_service import document_service
    from src.indexing.tasks import process_document
    
    record = document_service.get_document_status(document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Документ не найден")
    
    file_path = document_service._upload_dir / f"{document_id}_{record.filename}"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Исходный файл не найден")
    
    record.status = "pending"
    record.progress = 0
    document_service._save_document_to_db(document_id)
    
    task = process_document.delay(document_id)
    return {
        "status": "ok",
        "message": f"Переобработка запущена: {document_id}",
        "task_id": str(task.id)
    }
