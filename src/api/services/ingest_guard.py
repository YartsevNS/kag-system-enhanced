"""Предохранитель загрузки документов (переключатель админа).

Зачем: на тестовом/нагруженном стенде нужно ОДНИМ переключателем остановить
попадание новых документов в систему — вручную из UI/API, парсером сайтов
(watched_urls) и по RSS. Все эти пути сходятся в
`document_service.upload_document`, поэтому проверка живёт в одном месте, а этот
модуль даёт её в виде читаемой функции для API/UI/monitor'а.

Настройка: config_store `system/uploads` → {"blocked": bool, "message": str}.
Обработка уже загруженных документов управляется ОТДЕЛЬНОЙ настройкой
`system/processing` (другая галочка в админке) — эти два переключателя независимы.

Исключение IngestBlockedError поднимается из сервиса, чтобы вызывающий код
(API-роут, монитор) мог отдать понятную ошибку, а не «что-то пошло не так».
"""

from __future__ import annotations

from typing import Optional

DEFAULT_MESSAGE = "загрузка документов временно отключена администратором"


class IngestBlockedError(RuntimeError):
    """Загрузка документов запрещена настройкой system/uploads.blocked."""


def ingest_block_message() -> Optional[str]:
    """Текст причины, если загрузка запрещена; иначе None.

    Любая ошибка чтения конфига трактуется как «не запрещено»: блокировка не
    должна включаться сама собой из-за недоступной БД.
    """
    try:
        from src.api.services.config_store import config_store

        cfg = config_store.get("system", "uploads") or {}
        if isinstance(cfg, dict) and cfg.get("blocked"):
            return str(cfg.get("message") or "").strip() or DEFAULT_MESSAGE
    except Exception:
        return None
    return None


def ensure_ingest_allowed() -> None:
    """Поднять IngestBlockedError, если загрузка запрещена."""
    msg = ingest_block_message()
    if msg:
        raise IngestBlockedError(msg)
