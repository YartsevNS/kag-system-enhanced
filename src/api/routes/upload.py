"""
Маршруты для загрузки и обработки документов

Архитектура (как Paperless-ngx):
1. Клиент шлёт POST /api/v1/upload/ с multipart/form-data
2. Сервер читает файл в память (await file.read()) — быстро, не bottleneck
3. Валидация (размер, тип, безопасность)
4. document_service.upload_document() — хеширует, сохраняет на диск, проверяет дубликаты
5. Сразу ответ клиенту с document_id
6. Очередь FIFO (asyncio.Queue + пул воркеров) забирает обработку

Никакого temp/rename/staging — файл сразу в /app/data/uploads/.
Обработка асинхронная, не блокирует upload.
"""
import time

import os
import io
import uuid
import json as _json


async def _read_bytes(path) -> bytes:
    """Прочитать файл в отдельном потоке (в async-роуте синхронное чтение блокирует loop)."""
    def _read():
        with open(path, "rb") as f:
            return f.read()
    return await asyncio.to_thread(_read)


async def _append_bytes(path, chunk: bytes) -> None:
    """Дописать чанк в файл в отдельном потоке."""
    def _append():
        with open(path, "ab") as f:
            f.write(chunk)
    await asyncio.to_thread(_append)


async def _write_meta(path, meta: dict) -> None:
    """Записать метаданные сессии (TUS) в отдельном потоке."""
    def _write():
        with open(path, "w") as f:
            json.dump(meta, f)
    await asyncio.to_thread(_write)


def _parse_id_list(raw) -> list:
    """Разобрать список id (allow/deny).

    Единая реализация — document_access.parse_id_list: раньше эта логика была
    скопирована в трёх местах (список, ACL документа, фильтр прав).
    """
    return parse_id_list(raw)


def _json_parse_source(raw) -> str:
    """Из source_metadata (JSON-строка) достать source_name (источник web_monitor)."""
    if not raw:
        return ""
    if isinstance(raw, dict):
        return str(raw.get("source_name") or "")
    try:
        v = _json.loads(raw)
        if isinstance(v, dict):
            return str(v.get("source_name") or "")
    except Exception:
        pass
    return ""


import asyncio
import json
from pathlib import Path
from typing import Optional, List, Union
from pydantic import BaseModel
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Request, Response, Form
from loguru import logger

from src.models import DocumentStatus
from src.api.services.document_service import document_service
from src.api.services.chunk_order import chunk_seq_of
from src.api.services.archive_guard import (
    ArchiveRejected,
    check_limits,
    check_member_names,
    check_tar_members,
    safe_target,
)
from src.config import get_settings
from src.security.validator import SecurityValidator, SecurityValidationError
from src.api.middleware.auth_v2 import get_current_admin, get_current_user_optional
from src.database.user_models import User

# Celery задача обработки документов (вместо asyncio.Queue)
# QueueGuard — единая точка постановки задач с защитой от дублей.
# ВАЖНО: все постановки документов на обработку идут ТОЛЬКО через
# enqueue_document() (Redis-замок + проверка статуса), а не через
# process_document.delay() напрямую — иначе возможны дубли задач.
from src.indexing.queue_guard import enqueue_document
from src.api.services.document_access import (
    ensure_can_read,
    ensure_owner_or_admin,
    is_owner_or_admin,
    parse_id_list,
)

router = APIRouter()

# Лимит одного файла (1GB)
# Лимиты берём из настроек, а не держим литералами в роутере.
_settings = get_settings()
MAX_FILE_SIZE = _settings.MAX_FILE_SIZE
# Сколько чанков документа тянем из Qdrant за раз (scroll, не пагинация):
# при достижении лимита в ответе будет truncated=True, чтобы это не было тихой потерей.
CHUNKS_SCROLL_LIMIT = 10000
# /queue: сколько ждать ответа воркеров и сколько держать результат в кэше
QUEUE_INSPECT_TIMEOUT = 2.0
QUEUE_CACHE_TTL = 5.0
_QUEUE_CACHE: dict = {"at": 0.0, "inspect": None}

# Rate limiter: не более N запросов в минуту на upload
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


def _deny_if_uploads_blocked():
    """423, если админ отключил загрузку документов (system/uploads.blocked).

    Проверка нужна и в сервисе (единая точка для парсинга сайтов и RSS), и здесь:
    у API должен быть понятный отказ с кодом, а не 500 из глубины сервиса.
    """
    from src.api.services.ingest_guard import ingest_block_message

    msg = ingest_block_message()
    if msg:
        raise HTTPException(status_code=423, detail={
            "code": "UPLOADS_BLOCKED",
            "message": f"Загрузка документов отключена: {msg}",
        })

# Директория для TUS чанков (временные файлы)
TUS_DIR = Path(_settings.TUS_DIR)


# ============================================================
# TUS — resumable upload protocol (RFC-описание)
# Позволяет загружать файлы до 1GB по частям.
# ============================================================

TUS_DIR.mkdir(parents=True, exist_ok=True)


def _tus_meta_path(upload_id: str) -> Path:
    """Путь к файлу метаданных TUS-сессии."""
    return TUS_DIR / f"{upload_id}.meta"


def _tus_file_path(upload_id: str) -> Path:
    """Путь к файлу с частично загруженными данными."""
    return TUS_DIR / f"{upload_id}.bin"


def _tus_read_meta(upload_id: str) -> dict:
    """Метаданные TUS-сессии (пустой dict, если сессии нет)."""
    meta_path = _tus_meta_path(upload_id)
    if not meta_path.exists():
        return {}
    try:
        with open(meta_path) as f:
            return json.load(f)
    except Exception:
        return {}


def _tus_check_owner(meta: dict, current_user) -> None:
    """HEAD/PATCH/DELETE — только для своей сессии (или админу).

    Без этой проверки любой авторизованный пользователь, зная upload_id
    (он приходит в Location и попадает в логи), мог узнать offset чужой
    загрузки и удалить чужую сессию.
    """
    if not meta:
        raise HTTPException(status_code=404, detail="Upload session not found")
    owner = str(meta.get("uploaded_by") or "")
    if bool(getattr(current_user, "is_admin", False)):
        return
    if not current_user or not owner or owner != str(current_user.id):
        raise HTTPException(status_code=403, detail="Чужая сессия загрузки")


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
    # Заблокирована ли загрузка администратором: раньше TUS это игнорировал
    # (документ можно было загрузить в обход галочки «запретить загрузку»).
    _deny_if_uploads_blocked()

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
    await _write_meta(_tus_meta_path(upload_id), meta)

    # Создаём пустой файл для чанков (pre-allocate не обязателен)
    await asyncio.to_thread(_tus_file_path(upload_id).touch)

    logger.info(f"[TUS] Сессия создана: {upload_id}, файл: {filename}, размер: {total_size}")

    # Location — URL для последующих PATCH/HEAD запросов
    return Response(
        status_code=201,
        headers={
            "Location": f"/api/v1/upload/tus/{upload_id}",
            "Tus-Resumable": "1.0.0",
            "Upload-Offset": "0",
        },
    )


@router.head("/tus/{upload_id}")
async def tus_head(
    upload_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """TUS: получить текущий статус загрузки (сколько байт уже получено)."""
    meta = await asyncio.to_thread(_tus_read_meta, upload_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Upload session not found")

    _tus_check_owner(meta, current_user)

    def _offset() -> int:
        file_path = _tus_file_path(upload_id)
        return file_path.stat().st_size if file_path.exists() else 0

    offset = await asyncio.to_thread(_offset)

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

    meta = await asyncio.to_thread(_tus_read_meta, upload_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Upload session not found")

    _tus_check_owner(meta, current_user)

    file_path = _tus_file_path(upload_id)

    def _offset() -> int:
        return file_path.stat().st_size if file_path.exists() else 0

    current_offset = await asyncio.to_thread(_offset)

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

    await _append_bytes(file_path, chunk)
    new_offset = await asyncio.to_thread(lambda: file_path.stat().st_size)
    logger.debug(f"[TUS] Чанк получен: {upload_id}, offset: {current_offset}→{new_offset}")

    # Если всё загружено — завершаем сессию
    if new_offset >= meta["total_size"]:
        logger.info(f"[TUS] Загрузка завершена: {upload_id}, файл: {meta['filename']}")

        # Собираем файл и отправляем в document_service
        try:
            file_content = await _read_bytes(file_path)

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

            # Обработка НЕ запускается автоматически: запускается только
            # кнопкой «Обработать» на странице Документы (может быть
            # заблокирована администратором на тех. обслуживание).
            logger.info(f"[TUS] Документ сохранён, обработка отложена: {record.document_id}")

        except Exception as e:
            logger.error(f"[TUS] Ошибка финализации {upload_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            # Очищаем временные файлы
            await asyncio.to_thread(_cleanup_tus, upload_id)

    return Response(
        headers={
            "Upload-Offset": str(new_offset),
            "Tus-Resumable": "1.0.0",
        }
    )


@router.delete("/tus/{upload_id}", status_code=204)
async def tus_delete(
    upload_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """TUS: отменить и удалить сессию загрузки (только своей сессии)."""
    meta = await asyncio.to_thread(_tus_read_meta, upload_id)
    _tus_check_owner(meta, current_user)
    await asyncio.to_thread(_cleanup_tus, upload_id)
    # Явно: декоратор объявляет 204, но Response() по умолчанию отдаёт 200.
    return Response(status_code=204)


@router.post("/", response_model=DocumentStatus, summary="Загрузить документ")
async def upload_document(
    file: UploadFile = File(...),
    request: Request = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    # ── Права доступа (ACL): задаются при загрузке ──────────────────────
    visibility: str = Form("public"),
    allow_group_ids: str = Form("[]"),
    deny_group_ids: str = Form("[]"),
    allow_user_ids: str = Form("[]"),
    deny_user_ids: str = Form("[]"),
    # ── Источник (внешние поставщики: web_collector и т.п.) ────────────
    source_name: str = Form(""),
    source_url: str = Form(""),
):
    """
    Загрузить документ.

    Простая логика: прочитать файл → проверить → сохранить → ответить.
    Обработка (OCR, чанкинг, векторизация) — асинхронно через очередь.
    """
    upload_id = str(uuid.uuid4())
    filename = file.filename or f"unnamed_{upload_id[:8]}"

    _deny_if_uploads_blocked()

    # Rate limit
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    logger.info(f"[{upload_id}] 📥 Загрузка: {filename}, тип: {file.content_type}")

    # Читаем файл целиком
    try:
        content = await file.read()
    except HTTPException:
        raise
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
            upload_id=upload_id,
            source_metadata=(
                {"source_name": source_name, "source_url": source_url}
                if source_name or source_url
                else None
            ),
            access={
                "visibility": visibility if visibility in ("public", "restricted") else "public",
                "allow_group_ids": _parse_id_list(allow_group_ids),
                "deny_group_ids": _parse_id_list(deny_group_ids),
                "allow_user_ids": _parse_id_list(allow_user_ids),
                "deny_user_ids": _parse_id_list(deny_user_ids),
            }
        )

        # Обработка НЕ запускается автоматически (кнопка «Обработать» на
        # странице Документы; может быть заблокирована администратором).
        logger.info(f"[{upload_id}] 📋 Сохранён, обработка отложена")

        return DocumentStatus(
            document_id=record.document_id,
            status=record.status,
            progress=record.progress,
            upload_id=upload_id,
            created_at=record.created_at,
            updated_at=record.updated_at
        )

    except HTTPException:
        raise
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

    Каждый файл: прочитать → проверить → сохранить → в очередь.
    Если один файл упал — остальные продолжают.
    """
    # Rate limit (batch считается как 1 запрос)
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    _deny_if_uploads_blocked()

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

            # Обработка НЕ запускается автоматически (кнопка «Обработать»)
            logger.info(f"[batch] Сохранён, обработка отложена: {record.document_id}")

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

    _deny_if_uploads_blocked()

    upload_id = str(uuid.uuid4())
    filename = file.filename or f"archive_{upload_id[:8]}"
    logger.info(f"[{upload_id}] 📦 Загрузка архива: {filename}")

    # Читаем архив в память
    try:
        archive_bytes = await file.read()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка чтения архива: {e}")

    # Создаём временную папку для распаковки
    # Каталог распаковки — из настроек (раньше литерал /tmp).
    extract_dir = Path(_settings.UPLOAD_TEMP_DIR) / f"bulk_{upload_id}"
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
                infos = await asyncio.to_thread(zf.infolist)
                # Лимиты и имена проверяем ДО распаковки: иначе ZIP-бомба
                # (тысячи файлов или гигабайты нулей) успеет лечь на диск.
                check_limits(
                    [(i.filename, i.file_size) for i in infos],
                    len(archive_bytes),
                    _settings.MAX_ARCHIVE_ENTRIES,
                    _settings.MAX_ARCHIVE_UNCOMPRESSED,
                )
                check_member_names([i.filename for i in infos], extract_dir)
                for info in infos:
                    if info.filename.startswith("__") or info.filename.startswith("."):
                        continue
                    entry_ext = Path(info.filename).suffix.lower()
                    if entry_ext not in allowed_exts:
                        continue
                    target = safe_target(extract_dir, info.filename)
                    if target is None:
                        continue
                    # Извлекаем во временную папку (имя проверено выше)
                    await asyncio.to_thread(zf.extract, info, extract_dir)
                    extracted_path = target
                    if not extracted_path.is_file():
                        continue

                    # Загружаем каждый файл
                    await _process_bulk_file(
                        extracted_path, entry_ext, uploaded_by, group_ids,
                        upload_id, results
                    )

        elif ext in (".tar", ".gz", ".tgz"):
            import tarfile
                    # tarfile.open режим определяется по расширению
            mode = "r:gz" if ext in (".gz", ".tgz") else "r:"
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode=mode) as tf:
                members = await asyncio.to_thread(tf.getmembers)
                # tarfile.extract НЕ защищён от «../» (в отличие от zipfile с
                # Python 3.6) и позволяет symlink-атаку: проверяем имена, ссылки
                # и спецфайлы; лимиты — по суммарному размеру.
                check_limits(
                    [(m.name, m.size) for m in members],
                    len(archive_bytes),
                    _settings.MAX_ARCHIVE_ENTRIES,
                    _settings.MAX_ARCHIVE_UNCOMPRESSED,
                )
                check_tar_members(members, extract_dir)
                for member in members:
                    if not member.isfile():
                        continue
                    entry_ext = Path(member.name).suffix.lower()
                    if entry_ext not in allowed_exts:
                        continue
                    target = safe_target(extract_dir, member.name)
                    if target is None:
                        continue
                    await asyncio.to_thread(tf.extract, member, extract_dir)
                    extracted_path = target
                    if not extracted_path.is_file():
                        continue

                    await _process_bulk_file(
                        extracted_path, entry_ext, uploaded_by, group_ids,
                        upload_id, results
                    )
        else:
            raise HTTPException(status_code=400, detail=f"Неподдерживаемый формат архива: {ext}")

    except HTTPException:
        raise
    except ArchiveRejected as e:
        # Отказ по безопасности/лимитам — это не «ошибка распаковки»:
        # 400 для пустого/битого архива, 413 когда превышены лимиты.
        logger.warning(f"[{upload_id}] архив отклонён ({e.code}): {e.reason}")
        status = 400 if e.code in ("ARCHIVE_EMPTY",) else 413
        raise HTTPException(status_code=status, detail=f"Архив отклонён: {e.reason}")
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


async def _process_bulk_file(
    file_path: Path,
    file_ext: str,
    uploaded_by, group_ids, upload_id, results: list
):
    """Загрузить один файл из архива в document_service."""
    try:
        content = await _read_bytes(file_path)

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

        record = await document_service.upload_document(
            filename=file_path.name, file_content=content,
            file_type=file_ext, uploaded_by=uploaded_by,
            group_ids=group_ids, upload_id=upload_id,
        )

        # Обработка НЕ запускается автоматически (кнопка «Обработать»)
        logger.info(f"[bulk] Сохранён, обработка отложена: {record.document_id}")

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
async def get_document_status(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Получить статус обработки документа.

    - **document_id**: Идентификатор документа

    Статусы:
    - pending: Ожидает обработки
    - processing: В процессе обработки
    - completed: Обработка завершена
    - failed: Ошибка обработки
    """
    ensure_can_read(document_id, current_user)
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


@router.get("/list", summary="Список документов (с пагинацией)")
async def list_documents(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Получить список документов с пагинацией.
    
    - **limit**: Сколько записей (макс 1000)
    - **offset**: Сдвиг от начала
    - **status**: Фильтр по статусу (pending/processing/completed/error)
    """
    from src.api.services.document_repository import get_doc_repo

    repo = get_doc_repo()
    documents, total = repo.list(limit=min(limit, 1000), offset=offset, status=status)

    # ── Устранение N+1 запросов при загрузке страницы документов ────────
    # Раньше фронтенд на КАЖДЫЙ документ слал /upload/{id}/details
    # (enrichDocs): 145 документов = 145 HTTP + ~290 Qdrant scroll + 145 SQL.
    # Из details реально нужен только uploaded_by_name (имя загрузившего);
    # document_type/recognized_title/summary/topics уже есть в list из SQL,
    # а tags в системе не хранятся вовсе (Qdrant payload их не содержит).
    # Поэтому: имена пользователей берём ОДНИМ SQL-запросом (IN ...),
    # tags отдаём пустыми (фронтенд к этому готов — d.tags || []).
    user_names = {}
    try:
        user_ids = list({d.uploaded_by for d in documents if d.uploaded_by})
        if user_ids:
            from src.database.session import get_engine, get_session_local
            from src.database.user_models import User as UserModel
            get_engine()
            session = get_session_local()()
            rows = session.query(UserModel.id, UserModel.username).filter(
                UserModel.id.in_(user_ids)
            ).all()
            user_names = {uid: name for uid, name in rows}
            session.close()
    except Exception as e:
        logger.debug(f"Не удалось получить имена пользователей: {e}")

    enriched = []
    for d in documents:
        meta = repo.to_dict(d)
        did = d.id
        item = {
            "document_id": did,
            "filename": meta.get("filename", ""),
            "file_type": meta.get("file_type", "") or "",
            "file_size": meta.get("file_size", 0) or 0,
            "status": meta.get("status", "pending"),
            "progress": meta.get("progress", 0) or 0,
            "chunks_count": meta.get("chunks_count", 0) or 0,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            "uploaded_by": meta.get("uploaded_by"),
            "uploaded_by_name": user_names.get(meta.get("uploaded_by")),
            "is_active": meta.get("is_active", True),
            "group_ids": meta.get("group_ids", []),
            "file_hash": meta.get("file_hash", ""),
            "version": meta.get("version", 1),
            # Классификация (теперь в SQL)
            "document_type": meta.get("document_type", ""),
            "recognized_title": meta.get("recognized_title", ""),
            "summary": meta.get("summary", ""),
            "topics": meta.get("topics", []),
            # tags в системе не хранятся — пустой список (фронтенд готов)
            "tags": [],
            # Источник (web_monitor): из source_metadata JSON
            "source_name": _json_parse_source(meta.get("source_metadata")),
            # ACL (для фильтрации списка у не-admin)
            "visibility": meta.get("visibility") or "public",
            "allow_user_ids": _parse_id_list(meta.get("allow_user_ids")),
            "deny_user_ids": _parse_id_list(meta.get("deny_user_ids")),
            "allow_group_ids": _parse_id_list(meta.get("allow_group_ids")),
            "deny_group_ids": _parse_id_list(meta.get("deny_group_ids")),
        }
        enriched.append(item)

    # ── ACL-фильтрация списка (visibility + allow/deny) для не-admin ────
    # Логика та же, что в search/_access_guard: deny имеет приоритет;
    # restricted доступен только если пользователь/группа в allow.
    # Без user_id (аноним) — только public.
    if current_user is None:
        enriched = [d for d in enriched if (d.get("visibility") or "public") == "public"]
    elif not current_user.is_admin:
        uid = str(current_user.id)
        user_group_ids = [g.id for g in current_user.groups] if current_user.groups else []
        gset = set(user_group_ids)
        acl_filtered = []
        for d in enriched:
            v = d.get("visibility") or "public"
            deny_u = d.get("deny_user_ids") or []
            deny_g = d.get("deny_group_ids") or []
            # deny имеет приоритет над всем
            if uid in deny_u or (gset and gset & set(deny_g)):
                continue
            # legacy group_ids (payload-группы документа): если документ
            # привязан к группам, не-admin без этих групп его не видит
            doc_groups = d.get("group_ids") or []
            if doc_groups and not (gset & set(doc_groups)):
                continue
            if v == "public":
                acl_filtered.append(d)
                continue
            # restricted: доступен только если в allow
            allow_u = d.get("allow_user_ids") or []
            allow_g = d.get("allow_group_ids") or []
            if uid in allow_u or (gset and gset & set(allow_g)):
                acl_filtered.append(d)
        enriched = acl_filtered

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "documents": enriched,
    }


@router.delete("/{document_id}", summary="Удалить документ")
async def delete_document(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Удалить документ и его индексы (БД + Qdrant + Neo4j + файлы/миниатюры).

    Многопользовательность: удалять можно только свои документы (или админу).
    """
    # Проверка владельца
    from src.api.services.document_repository import get_doc_repo
    doc = get_doc_repo().get(document_id)
    if doc is not None:
        owner = getattr(doc, "uploaded_by", None)
        is_admin = bool(current_user and getattr(current_user, "is_admin", False))
        if owner and (not current_user or str(owner) != str(current_user.id)) and not is_admin:
            raise HTTPException(
                status_code=403,
                detail="Недостаточно прав: можно удалять только свои документы",
            )
    elif current_user is None:
        raise HTTPException(status_code=401, detail="Требуется аутентификация")

    success = await document_service.delete_document(document_id)

    if not success:
        raise HTTPException(status_code=500, detail="Не удалось удалить документ")

    return {"status": "ok", "document_id": document_id}


# ═══════════════════════════════════════
# Права доступа (ACL) — настраиваются при загрузке/редактировании документа
# ═══════════════════════════════════════

@router.get("/access-options", summary="Группы и пользователи для формы прав")
async def get_access_options():
    """Списки групп и пользователей для формы прав документа (страница «Документы»)."""
    try:
        from src.database.session import get_session_local
        from src.database.user_models import User, Group
        maker = get_session_local()
        s = maker()
        try:
            groups = [{"id": g.id, "name": g.name} for g in s.query(Group).all()]
            users = [{"id": u.id, "username": u.username} for u in s.query(User).all() if not u.is_admin]
        finally:
            s.close()
        return {"groups": groups, "users": users}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/{document_id}/access", summary="Права доступа документа")
async def get_document_access(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    from src.api.services.document_repository import get_doc_repo
    ensure_can_read(document_id, current_user)
    doc = get_doc_repo().get(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return {
        "visibility": doc.visibility or "public",
        "allow_group_ids": _parse_id_list(doc.allow_group_ids or "[]"),
        "deny_group_ids": _parse_id_list(doc.deny_group_ids or "[]"),
        "allow_user_ids": _parse_id_list(doc.allow_user_ids or "[]"),
        "deny_user_ids": _parse_id_list(doc.deny_user_ids or "[]"),
    }


class DocumentAccessUpdate(BaseModel):
    """Права доступа документа (частичное обновление).

    Списки приходят и массивами, и JSON-строками (форма загрузки отправляет
    строки, модалка «Права» — массив), поэтому тип — Union[list, str];
    разбор в единую форму делает parse_id_list.
    """

    visibility: Optional[str] = None
    allow_group_ids: Optional[Union[List[str], str]] = None
    deny_group_ids: Optional[Union[List[str], str]] = None
    allow_user_ids: Optional[Union[List[str], str]] = None
    deny_user_ids: Optional[Union[List[str], str]] = None


@router.put("/{document_id}/access", summary="Сохранить права доступа документа")
async def update_document_access(
    document_id: str,
    payload: DocumentAccessUpdate,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Обновить права документа и payload чанков в Qdrant (без переиндексации текста).

    Многопользовательность: права может менять владелец документа (uploaded_by)
    или админ. Системные документы без владельца — любой авторизованный.
    """
    data = payload.model_dump(exclude_unset=True)
    try:
        from src.api.services.document_repository import get_doc_repo
        doc = get_doc_repo().get(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Документ не найден")

        # Проверка владельца: свои документы — владельцу, системные без
        # владельца — только админу (как в /upload/{id}/process)
        owner = getattr(doc, "uploaded_by", None)
        is_admin = bool(current_user and getattr(current_user, "is_admin", False))
        if current_user is None:
            raise HTTPException(status_code=401, detail="Требуется аутентификация")
        if not is_admin and str(owner or "") != str(current_user.id):
            raise HTTPException(
                status_code=403,
                detail="Недостаточно прав: можно менять права только своих документов",
            )

        # Частичное обновление: непереданное поле сохраняет текущее значение
        # (раньше отсутствие поля в теле молча сбрасывало соответствующий список).
        access = {
            "visibility": data.get("visibility", doc.visibility or "public"),
            "allow_group_ids": data.get("allow_group_ids", _parse_id_list(doc.allow_group_ids)),
            "deny_group_ids": data.get("deny_group_ids", _parse_id_list(doc.deny_group_ids)),
            "allow_user_ids": data.get("allow_user_ids", _parse_id_list(doc.allow_user_ids)),
            "deny_user_ids": data.get("deny_user_ids", _parse_id_list(doc.deny_user_ids)),
        }
        if access["visibility"] not in ("public", "restricted"):
            access["visibility"] = "public"

        get_doc_repo().upsert(document_id, {
            "visibility": access["visibility"],
            "allow_group_ids": access["allow_group_ids"],
            "deny_group_ids": access["deny_group_ids"],
            "allow_user_ids": access["allow_user_ids"],
            "deny_user_ids": access["deny_user_ids"],
        })

        # Обновляем payload чанков в Qdrant (быстро, без переиндексации)
        try:
            from src.indexing.embeddings_service import embeddings_service
            await embeddings_service.set_document_access(document_id, access)
        except Exception as e:
            logger.warning(f"payload access не обновлён: {e}")

        return {"status": "ok", **access}
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/{document_id}/tables", summary="Таблицы документа (структурно)")
async def get_document_tables(
    document_id: str,
    limit: int = 100,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Список таблиц документа из document_tables (rows, headers, markdown, html).

    Для точных запросов («что в таблице по ОКВЭД 61.10.1») и рендера в чате.
    """
    ensure_can_read(document_id, current_user)
    try:
        from src.database.session import get_session_local
        from src.database.document_table_models import DocumentTable
        maker = get_session_local()
        session = maker()
        try:
            rows = session.query(DocumentTable).filter_by(document_id=document_id) \
                .order_by(DocumentTable.page_num, DocumentTable.table_index).limit(limit).all()
            return {"status": "ok", "document_id": document_id, "total": len(rows),
                    "tables": [r.to_dict() for r in rows]}
        finally:
            session.close()
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/{document_id}/tables/search", summary="Поиск по таблицам документа")
async def search_document_tables(
    document_id: str,
    query: str = "",
    column: str = "",
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Поиск значения в таблицах документа (точный, по ячейкам).

    query — искомый текст в ячейках; column — фильтр по заголовку колонки.
    Возвращает найденные строки (целиком) — для точных ответов в чате.
    """
    ensure_can_read(document_id, current_user)
    try:
        from src.database.session import get_session_local
        from src.database.document_table_models import DocumentTable
        maker = get_session_local()
        session = maker()
        try:
            tables = session.query(DocumentTable).filter_by(document_id=document_id).all()
            q = (query or "").strip().lower()
            col = (column or "").strip().lower()
            matches = []
            for t in tables:
                rows = t.to_dict()
                headers = [h.lower() for h in rows.get("headers", [])]
                col_idx = None
                if col:
                    try:
                        col_idx = headers.index(col)
                    except ValueError:
                        col_idx = None
                for ri, r in enumerate(rows.get("rows", [])):
                    if not q:
                        continue
                    if col_idx is not None:
                        cell = str(r[col_idx] if col_idx < len(r) else "")
                        if q in cell.lower():
                            matches.append({"table_id": t.id, "page": t.page_num,
                                            "row_index": ri, "row": r,
                                            "headers": rows.get("headers", [])})
                    else:
                        joined = " ".join(str(c) for c in r).lower()
                        if q in joined:
                            matches.append({"table_id": t.id, "page": t.page_num,
                                            "row_index": ri, "row": r,
                                            "headers": rows.get("headers", [])})
            # лимит
            matches = matches[:50]
            return {"status": "ok", "found": len(matches), "matches": matches}
        finally:
            session.close()
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/{document_id}/reindex", summary="Переиндексировать документ")
async def reindex_document(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Поставить документ на переиндексацию (заново создать вектора).

    Работа ставится в очередь Celery, а не выполняется в запросе: раньше здесь
    был синхронный `document_service.process_document(..., force=True)`, и на
    большом документе это минуты удержания HTTP-соединения. Клиентский таймаут
    обрывал работу на середине (документ оставался в статусе processing), а если
    параллельно работала задача worker'а, граф Neo4j писали два процесса сразу
    (дедлок — известный питфолл проекта).

    force=True обязателен: без него старые чанки остаются в Qdrant/Neo4j, новые
    добавляются рядом и в поиске появляются дубли (баг 2026-09-12: два point_id
    на один chunk_id).
    """
    from src.indexing.queue_guard import enqueue_document

    # Переиндексация — тяжёлая операция над конкретным документом: как и
    # /{id}/process, её может запускать владелец или админ (раньше — любой
    # авторизованный, то есть чужой документ можно было переиндексировать).
    ensure_owner_or_admin(document_id, current_user)

    # False = задача для документа уже стоит (QueueGuard). Отвечаем честно,
    # а не «ok», как раньше с task_id="duplicate_skipped".
    if not enqueue_document(document_id, force=True):
        return {
            "status": "already_queued",
            "document_id": document_id,
            "message": "Документ уже в очереди обработки — переиндексация не запускалась",
        }
    return {
        "status": "queued",
        "document_id": document_id,
        "message": "Переиндексация поставлена в очередь",
    }


@router.post("/{document_id}/process", summary="Запустить обработку документа (кнопка «Обработать»)")
async def process_document_now(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Поставить документ в очередь обработки (OCR → чанки → векторы → граф).

    Многопользовательность: запускать обработку можно ТОЛЬКО своих документов
    (uploaded_by == текущий пользователь) или админу — чужие документы
    не обрабатываются с одного нажатия. Плюс блокировка администратором
    (config_store system/processing blocked=true) на время тех. обслуживания.
    """
    from src.api.services.config_store import config_store

    # Блокировка запуска обработки из админки
    cfg = config_store.get("system", "processing") or {}
    if isinstance(cfg, dict) and cfg.get("blocked"):
        msg = str(cfg.get("message", "")) or "обработка временно недоступна (техническое обслуживание)"
        raise HTTPException(
            status_code=423,
            detail=f"Обработка заблокирована администратором: {msg}. Обратитесь к администратору.",
        )

    # Многопользовательность: только свои документы (или админ)
    from src.api.services.document_repository import get_doc_repo
    doc = get_doc_repo().get(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")
    owner = getattr(doc, "uploaded_by", None)
    is_admin = bool(current_user and getattr(current_user, "is_admin", False))
    if not current_user and not is_admin:
        # Раньше при owner=None проверка «коротко замыкалась» и пропускала
        # не-админа к системному документу: выполняем требование явно.
        raise HTTPException(status_code=401, detail="Требуется аутентификация")
    if owner and str(owner) != str(current_user.id) and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Недостаточно прав: можно запускать обработку только своих документов",
        )
    if not owner and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Недостаточно прав: системный документ может обработать только администратор",
        )

    try:
        from src.indexing.queue_guard import enqueue_document
        # enqueue_document возвращает False, когда задача для документа уже
        # стоит (Redis-замок QueueGuard занят): раньше роут всё равно отвечал
        # «поставлено», и нажатие «Обработать» выглядело успешным, ничего не
        # запуская. Теперь это честный 409 — как у /rebuild-graph.
        if not enqueue_document(document_id, force=True):
            raise HTTPException(
                status_code=409,
                detail="Документ уже в очереди обработки (или обрабатывается) — повторная постановка не нужна",
            )
        return {"status": "queued", "document_id": document_id,
                "message": "Документ поставлен в очередь обработки"}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Файл документа не найден")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/process-all", summary="Обработать все необработанные документы пользователя")
async def process_all_mine(current_user: Optional[User] = Depends(get_current_user_optional)):
    """Поставить в очередь ВСЕ необработанные документы текущего пользователя.

    - обычный пользователь: только свои документы (pending/failed, uploaded_by == id)
    - админ: все pending/failed (включая системные без владельца)
    Уважает блокировку обработки из админки (423).
    """
    from src.api.services.config_store import config_store

    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется аутентификация")

    cfg = config_store.get("system", "processing") or {}
    if isinstance(cfg, dict) and cfg.get("blocked"):
        msg = str(cfg.get("message", "")) or "обработка временно недоступна (техническое обслуживание)"
        raise HTTPException(
            status_code=423,
            detail=f"Обработка заблокирована администратором: {msg}. Обратитесь к администратору.",
        )

    from src.api.services.document_repository import get_doc_repo
    docs = get_doc_repo().get_all() or {}
    is_admin = bool(getattr(current_user, "is_admin", False))

    targets = []
    for did, meta in docs.items():
        status = meta.get("status") if isinstance(meta, dict) else getattr(meta, "status", None)
        if status not in ("pending", "failed"):
            continue
        owner = meta.get("uploaded_by") if isinstance(meta, dict) else getattr(meta, "uploaded_by", None)
        if is_admin:
            targets.append(did)  # админ — любые (включая системные без владельца)
        elif owner and str(owner) == str(current_user.id):
            targets.append(did)  # только свои
        elif not owner:
            continue  # системные/чужие обычному пользователю не трогаем

    if not targets:
        return {"status": "ok", "queued": 0, "total_pending": 0,
                "message": "Нет необработанных документов"}

    from src.indexing.queue_guard import enqueue_document
    queued = 0
    skipped = 0
    for did in targets:
        try:
            # False = задача для документа уже стоит (QueueGuard): считаем
            # отдельно, иначе счётчик врал («поставлено 5», а постановок 3).
            if enqueue_document(did, force=True):
                queued += 1
            else:
                skipped += 1
                logger.info(f"process-all: {did} уже в очереди — повтор не нужен")
        except Exception as e:
            logger.warning(f"process-all: не удалось поставить {did}: {e}")

    message = f"Поставлено в очередь: {queued} из {len(targets)}"
    if skipped:
        message += f" (уже были в очереди: {skipped})"
    return {"status": "ok", "queued": queued, "skipped": skipped,
            "total_pending": len(targets), "message": message}


@router.get("/{document_id}/chunks", summary="Чанки документа")
async def get_document_chunks(
    document_id: str,
    offset: int = 0,
    limit: int = 10,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Получить чанки конкретного документа из Qdrant."""
    ensure_can_read(document_id, current_user)
    from src.indexing.qdrant_service import get_qdrant_service
    qdrant_service = get_qdrant_service()
    
    try:
        # Получаем ВСЕ чанки документа (Qdrant scroll возвращает неупорядоченно)
        # Qdrant-клиент синхронный: вызов в потоке, иначе loop ждёт сеть.
        all_results = await asyncio.to_thread(
            qdrant_service.scroll_points,
            filter={
                "must": [{"key": "document_id", "match": {"value": document_id}}]
            },
            limit=CHUNKS_SCROLL_LIMIT,
        )
        
        truncated = len(all_results) >= CHUNKS_SCROLL_LIMIT
        all_chunks = []
        for r in all_results:
            payload = r.get("payload", {})
            seq = chunk_seq_of(payload)
            all_chunks.append({
                "id": r.get("id", ""),
                "chunk_id": payload.get("chunk_id", ""),
                "text": payload.get("text", payload.get("content", "")),
                "chunk_index": payload.get("chunk_index", (payload.get("metadata") or {}).get("chunk_index", 0)),
                "chunk_seq": seq,
                "metadata": payload.get("metadata", {}),
                "document_id": document_id
            })

        # Сортируем по номеру чанка (см. src/api/services/chunk_order.py — общий
        # разбор: номер лежит в metadata, а не на верхнем уровне payload).
        all_chunks.sort(key=lambda c: (c.get("chunk_seq") or 10 ** 9,
                                       str(c.get("chunk_id") or "")))

        total = len(all_chunks)
        chunks = all_chunks[offset:offset + limit]
        
        return {
            "chunks": chunks, "total": total, "offset": offset, "limit": limit,
            # truncated: в Qdrant за один scroll взяли CHUNKS_SCROLL_LIMIT
            # записей — если документ больше, это видно клиенту, а не молча теряется.
            "truncated": truncated, "scroll_limit": CHUNKS_SCROLL_LIMIT,
        }
    except Exception as e:
        logger.error(f"Ошибка получения чанков: {e}")
        return {"chunks": [], "total": 0, "error": str(e)}


@router.get("/{document_id}/details", summary="Детальная информация о документе")
async def get_document_details(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Получить расширенную информацию о документе с тэгами, типом, пользователем."""
    ensure_can_read(document_id, current_user)
    from src.api.services.document_repository import get_doc_repo
    from src.indexing.auto_tagger import get_auto_tagger

    # Первичный источник — document_service (in-memory, всегда актуальный)
    record = document_service.get_document_status(document_id)
    
    # Получаем запись из SQL (DocumentRepository)
    record_data = None
    try:
        # Чтение из БД — в потоке: в async-роуте это блокирует event loop.
        record_data = await asyncio.to_thread(get_doc_repo().get_dict, document_id)
    except Exception:
        record_data = None
    
    if not record and not record_data:
        raise HTTPException(status_code=404, detail="Документ не найден")

    # Приоритет: in-memory record > SQL
    filename = record.filename if record else record_data.get("filename", "unknown")
    file_type = record.file_type if record else record_data.get("file_type", "unknown")
    file_size = record.file_size if record else record_data.get("file_size", 0)
    file_hash = record_data.get("file_hash", "") if record_data else ""
    status = record.status if record else record_data.get("status", "unknown")
    uploaded_by = record.uploaded_by if record else record_data.get("uploaded_by")
    created_at_raw = record.created_at if record else record_data.get("created_at")
    updated_at_raw = record.updated_at if record else record_data.get("updated_at")
    
    # chunks_count: приоритет in-memory, затем SQL, затем 0
    chunks_count = record.chunks_count if record else record_data.get("chunks_count", 0) if record_data else 0
    
    # Если in-memory показывает 0, но в Qdrant есть чанки — обновим
    if chunks_count == 0:
        try:
            # Точный count вместо двух scroll (раньше: limit=1 «проверить» +
            # limit=100000 «посчитать» — то есть выгрузка всех точек документа
            # на каждый показ карточки).
            from src.indexing.embeddings_service import embeddings_service
            real_count = await embeddings_service.count_document_points(document_id)
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
    
    # document_type и recognized_title: приоритет in-memory, затем SQL
    doc_type_inmem = getattr(record, 'document_type', None) if record else None
    doc_title_inmem = getattr(record, 'recognized_title', None) if record else None
    cfg_document_type = doc_type_inmem or (record_data.get('document_type') if record_data else None)
    cfg_recognized_title = doc_title_inmem or (record_data.get('recognized_title') if record_data else None)

    # Get user info
    uploaded_by_name = None
    if uploaded_by:
        try:
            from src.database.session import get_engine, get_session_local
            from src.database.user_models import User as UserModel
            get_engine()
            session = get_session_local()()
            user = await asyncio.to_thread(
                lambda: session.query(UserModel).filter(UserModel.id == uploaded_by).first()
            )
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
        results = await asyncio.to_thread(
            qdrant_service.scroll_points,
            filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
            limit=1,
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
            results = await asyncio.to_thread(
                qdrant_service.scroll_points,
                filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
                limit=3,
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


class DocumentMetaUpdate(BaseModel):
    """Обновление метаданных документа: название и/или тип."""
    filename: Optional[str] = None
    document_type: Optional[str] = None


@router.patch("/{document_id}/meta", summary="Обновить название/тип документа")
async def update_document_meta(document_id: str, body: DocumentMetaUpdate):
    """Обновить filename и/или document_type документа.

    - БД: upsert (filename, document_type)
    - Qdrant: при смене типа — update_document_type_payload (все чанки)

    Зачем: на странице просмотра документа (viewer) админ может поправить
    название (распознанное неверно) и тип (LLM-типизация ошиблась).
    """
    from src.api.services.document_repository import get_doc_repo
    repo = get_doc_repo()
    if not await asyncio.to_thread(repo.get, document_id):
        raise HTTPException(status_code=404, detail="Документ не найден")

    data = {}
    if body.filename is not None and body.filename.strip():
        data["filename"] = body.filename.strip()
    if body.document_type is not None and body.document_type.strip():
        data["document_type"] = body.document_type.strip()

    if not data:
        return {"status": "ok", "message": "Ничего не изменено"}

    await asyncio.to_thread(repo.upsert, document_id, data)

    # При смене типа — обновляем payload всех чанков в Qdrant
    if "document_type" in data:
        try:
            from src.indexing.embeddings_service import embeddings_service
            await embeddings_service.initialize()
            await embeddings_service.update_document_type_payload(
                document_id, data["document_type"])
        except Exception as e:
            logger.warning(f"Qdrant document_type не обновлён: {e}")

    return {"status": "ok", "document_id": document_id, **data}


@router.get("/{document_id}/thumbnail", summary="Миниатюра документа")
async def get_document_thumbnail(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Вернуть миниатюру документа (WebP/Png), с кэшированием."""
    ensure_can_read(document_id, current_user)
    from pathlib import Path
    from fastapi.responses import FileResponse, Response
    
    thumb_dir = Path(_settings.THUMBNAILS_DIR)
    thumb_path = thumb_dir / f"{document_id}.webp"

    # ФС-операции — в потоке (см. also find_file: без перебора каталога).
    if await asyncio.to_thread(thumb_path.exists):
        return FileResponse(thumb_path, media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"})
    
    # Пробуем сгенерировать на лету
    try:
        # Точный путь по <document_id>_<имя>; перебор каталогов остался внутри
        # find_file только как fallback для старых имён.
        file_path = await asyncio.to_thread(document_service.find_file, document_id)
        if file_path:
            # Генерация миниатюры синхронная (Pillow/PyMuPDF) — в поток.
            thumb = await asyncio.to_thread(
                document_service._generate_thumbnail, document_id, file_path
            )
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
async def get_document_preview(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Вернуть файл документа для inline-просмотра в браузере."""
    ensure_can_read(document_id, current_user)
    from pathlib import Path
    from fastapi.responses import FileResponse
    
    # Точный путь по <document_id>_<имя> вместо перебора каталога
    file_path = await asyncio.to_thread(document_service.find_file, document_id)
    if not file_path:
        raise HTTPException(status_code=404, detail="Файл не найден")

    # MIME по расширению, а не «pdf или octet-stream»
    import mimetypes
    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type,
        headers={"Content-Disposition": "inline", "Cache-Control": "public, max-age=3600"})


@router.post("/reanalyze-all", summary="Переанализировать все документы")
async def reanalyze_all_documents(
    current_user: User = Depends(get_current_admin),
):
    """
    Фоновый переанализ всех completed-документов через LLM.
    Определяет document_type, recognized_title, summary, topics.
    """
    try:
        from src.api.services.document_analyzer import document_analyzer
        from src.api.services.document_repository import get_doc_repo
        from src.api.services.document_service import document_service
        
        all_docs = get_doc_repo().get_all() or {}
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


@router.post("/reindex-all", summary="Переиндексировать все документы (пересоздать embeddings)")
async def reindex_all_documents(
    current_user: User = Depends(get_current_admin),
):
    """
    Переиндексировать ВСЕ completed-документы — заново прогнать парсинг → чанкинг
    → векторизацию. Нужно после смены embedding-модели или исправления схемы Qdrant.
    Работает асинхронно через Celery (не блокирует API).
    """
    try:
        from src.api.services.document_repository import get_doc_repo

        all_docs = get_doc_repo().get_all() or {}
        ids = [
            did for did, doc in all_docs.items()
            if isinstance(doc, dict) and doc.get("status") == "completed"
        ]
        queued = 0
        for did in ids:
            # QueueGuard: force=True — это осознанная принудительная
            # переиндексация completed-документов (смена модели и т.п.).
            # Без force= completed-документ не был бы переставлен.
            # Считаем только реальные постановки: False = уже в очереди.
            if enqueue_document(did, force=True):
                queued += 1

        return {
            "status": "ok",
            "message": f"Поставлено в очередь на переиндексацию: {queued} из {len(ids)} документов",
            "total": len(ids),
            "queued": queued,
            "skipped": len(ids) - queued,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# Версионность и контроль дубликатов
# ============================================================
# Версионность и контроль дубликатов
# ============================================================

@router.get("/{document_id}/versions", summary="История версий документа")
async def get_document_versions(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Получить информацию о версиях документа.
    
    Возвращает текущую версию, хеш, хеш предыдущей версии,
    и флаг has_previous указывающий, есть ли с чем сравнивать.
    """
    ensure_can_read(document_id, current_user)
    try:
        from src.api.services.document_service import document_service
        from src.api.services.document_repository import get_doc_repo

        # Получаем из кэша или БД
        record = document_service._documents.get(document_id)
        if not record:
            meta = get_doc_repo().get_dict(document_id)
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
async def diff_document_versions(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Сравнить текущую версию документа с предыдущей.
    
    Возвращает diff: что изменилось в тексте между версиями.
    Полезно при повторной загрузке обновлённого документа.
    """
    ensure_can_read(document_id, current_user)
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
async def reprocess_pending_documents(
    current_user: User = Depends(get_current_admin),
):
    """
    Найти все документы со статусом 'pending' и запустить их обработку заново.
    Полезно после падения контейнера (OOM) — pending-документы остались без обработки.
    """
    from src.api.services.config_store import config_store
    from src.api.services.document_repository import get_doc_repo

    # Уважаем блокировку обработки из админки (тех. обслуживание)
    cfg = config_store.get("system", "processing") or {}
    if isinstance(cfg, dict) and cfg.get("blocked"):
        msg = str(cfg.get("message", "")) or "обработка временно недоступна (техническое обслуживание)"
        raise HTTPException(
            status_code=423,
            detail=f"Обработка заблокирована администратором: {msg}. Обратитесь к администратору.",
        )

    docs = get_doc_repo().get_all() or {}
    pending = [(did, doc) for did, doc in docs.items() 
               if isinstance(doc, dict) and doc.get('status') == 'pending']
    
    if not pending:
        return {"success": True, "message": "Нет pending-документов", "count": 0}
    
    logger.info(f"Запускаю переобработку {len(pending)} pending-документов")
    # Постановка в очередь СИНХРОННАЯ (Redis-замок QueueGuard), поэтому
    # asyncio.create_task здесь ничего не давало: ссылку на задачу никто не
    # сохранял (её мог собрать GC), а count увеличивался до фактической
    # постановки. Ставим задачу напрямую и считаем только успешные.
    count = 0
    skipped = 0
    for did, doc in pending:
        try:
            if enqueue_document(did, force=True):
                count += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning(f"Не удалось запустить {did}: {e}")
    
    message = f"Запущена переобработка: {count}"
    if skipped:
        message += f" (уже в очереди: {skipped})"
    return {"success": True, "message": message, "count": count, "skipped": skipped}


@router.get("/queue", summary="Статус очереди обработки")
async def queue_status():
    """
    Мониторинг очереди Celery: длина, активные задачи, воркеры.
    
    Returns:
        Статус очереди обработки документов
    """
    try:
        from src.indexing.celery_app import celery_app
        
        # inspect() ждёт ответа воркеров (в замере — до 3 с), поэтому:
        # 1) короткий таймаут ответа, 2) кэш на несколько секунд — дашборду
        # точность до секунд не нужна, а воркеров такой опрос не дёргает.
        now = time.monotonic()
        cached = _QUEUE_CACHE["inspect"]
        if cached and now - _QUEUE_CACHE["at"] < QUEUE_CACHE_TTL:
            active_tasks, reserved_tasks, scheduled_tasks = cached
        else:
            def _inspect():
                i = celery_app.control.inspect(timeout=QUEUE_INSPECT_TIMEOUT)
                return i.active() or {}, i.reserved() or {}, i.scheduled() or {}

            active_tasks, reserved_tasks, scheduled_tasks = await asyncio.to_thread(_inspect)
            _QUEUE_CACHE.update(at=now, inspect=(active_tasks, reserved_tasks, scheduled_tasks))
        
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


@router.get("/{document_id}/ocr", summary="Проверить наличие OCR/Markdown")
async def check_ocr(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Проверяет, есть ли распознанный Markdown-файл для документа."""
    ensure_can_read(document_id, current_user)
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
async def view_ocr_markdown(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Возвращает содержимое распознанного Markdown-файла."""
    ensure_can_read(document_id, current_user)
    from src.api.services.document_service import document_service
    from fastapi.responses import PlainTextResponse
    record = document_service.get_document_status(document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Документ не найден")
    md_path = document_service._ocr_dir / f"{record.filename}.md"
    if not md_path.exists():
        raise HTTPException(status_code=404, detail="Markdown не найден")
    # Markdown после OCR бывает на сотни КБ — читаем в потоке.
    text = await asyncio.to_thread(md_path.read_text, encoding="utf-8")
    return PlainTextResponse(text, media_type="text/markdown")



@router.post("/{document_id}/reprocess-ocr", summary="Пересоздать OCR/Markdown для документа")
async def reprocess_ocr(
    document_id: str,
    current_user: User = Depends(get_current_admin),
):
    """Принудительно перезапускает OCR и создание Markdown для документа."""
    from src.api.services.document_service import document_service
    
    record = document_service.get_document_status(document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Документ не найден")
    
    file_path = document_service._upload_dir / f"{document_id}_{record.filename}"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Исходный файл не найден")
    
    record.status = "pending"
    record.progress = 0
    document_service._save_document_to_db(document_id)
    
    # QueueGuard: force=True — это осознанный ручной перезапуск (reprocess),
    # поэтому разрешаем постановку, даже если документ был completed.
    queued = enqueue_document(document_id, force=True)
    if not queued:
        # Документ уже в очереди: раньше здесь возвращалось "status": "ok" с
        # "task_id": "duplicate_skipped" — читающий видел успех и не понимал,
        # что переобработки не будет.
        return {
            "status": "already_queued",
            "message": f"Документ уже в очереди обработки: {document_id}",
            "document_id": document_id,
        }
    return {
        "status": "ok",
        "message": f"Переобработка запущена: {document_id}",
        "document_id": document_id,
        "queued": True,
    }
