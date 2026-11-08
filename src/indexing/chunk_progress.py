"""Учёт обработанных чанков документа: докачка после сбоев без повторного LLM.

Зачем: обработка большого документа идёт минутами. Если она упала на середине,
раньше при повторе все чанки уходили в LLM заново — это дорого и долго.
Теперь каждый успешно записанный чанк отмечается, и следующий запуск начинает
с неотмеченных.

Хранение — Redis (SET), ключ `rg:doc:{document_id}:done`, TTL 7 дней:
  - переживает перезапуск воркера и api;
  - не требует миграции PostgreSQL;
  - TTL ограничивает мусор: документ, обработанный неделю назад, при повторе
    пойдёт целиком, если прогресс не нужен.

Правило: отметка ставится ТОЛЬКО после успешной записи в TypeDB. Ошибка записи
оставляет чанк неотмеченным — он повторится.
"""
from __future__ import annotations

import logging
from typing import Set

logger = logging.getLogger(__name__)

TTL_SECONDS = 7 * 24 * 3600


def _key(document_id: str) -> str:
    return f"rg:doc:{document_id}:done"


def _redis():
    import redis as redis_lib

    from src.config import get_settings

    s = get_settings()
    return redis_lib.Redis(host=s.REDIS_HOST, port=s.REDIS_PORT, db=1,
                           password=s.REDIS_PASSWORD or None, decode_responses=True)


def done_chunks(document_id: str) -> Set[str]:
    """Какие чанки документа уже записаны (пустое множество при недоступном Redis)."""
    try:
        return set(_redis().smembers(_key(document_id)))
    except Exception as exc:  # noqa: BLE001 — прогресс не критичен для работы
        logger.debug("прогресс чанков недоступен: %s", str(exc)[:120])
        return set()


def mark_done(document_id: str, chunk_id: str) -> None:
    try:
        r = _redis()
        r.sadd(_key(document_id), chunk_id)
        r.expire(_key(document_id), TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("отметка чанка не сохранена: %s", str(exc)[:120])


def clear(document_id: str) -> None:
    """Сбросить прогресс: нужен при принудительной переобработке (reindex)."""
    try:
        _redis().delete(_key(document_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("сброс прогресса не выполнен: %s", str(exc)[:120])


def summary(document_id: str, total: int) -> dict:
    done = done_chunks(document_id)
    return {"total": total, "done": len(done & {str(i) for i in range(total)}) if total else len(done),
            "done_chunks": sorted(done)[:5]}
