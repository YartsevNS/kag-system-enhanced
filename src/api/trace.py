"""Сквозной идентификатор запроса (trace_id): заголовок, контекст, журналы, метрики.

Зачем: сейчас, чтобы разобрать «почему ответ был плохим», приходится грепать логи по времени и
искать, какие строки относятся к одному запросу. С trace_id достаточно одного фильтра. Правило
простое: идентификатор рождается на входе API (или берётся из заголовка X-Trace-ID), кладётся в
ContextVar — оттуда его автоматически подставляет loguru в КАЖДУЮ строку журнала, включая строки
из потоков (`asyncio.to_thread` копирует контекст) — и возвращается клиенту в заголовке ответа.

Что даёт на практике:
  * `curl -i` показывает X-Trace-ID ответа — его можно назвать в поддержку;
  * `docker logs kag-api | grep <trace_id>` показывает весь путь запроса: поиск в Qdrant, граф,
    табличный слой, вызов модели, отсечение фрагментов, итоговое время;
  * в метриках тот же путь виден как гистограмма по стадиям.
"""

from __future__ import annotations

import re
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Optional

from loguru import logger

# Значение по умолчанию: запрос вне HTTP-контекста (воркер, скрипт) — в журнале будет «-».
_TRACE_ID: ContextVar[str] = ContextVar("trace_id", default="-")
_configured = False

TRACE_HEADER = "X-Trace-ID"


# Допустимые символы в идентификаторе запроса: только ASCII-«токен». Это не придирка —
# заголовки HTTP кодируются latin-1, и кириллица в X-Trace-ID падает на этапе отдачи ответа
# (UnicodeEncodeError, поймано тестом). Заодно это защита от подстановки переносов строк в журнал.
_TRACE_ALLOWED = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def new_trace_id() -> str:
    """Новый идентификатор запроса (короткий, но уникальный: 12 символов uuid4-hex)."""
    return uuid.uuid4().hex[:12]


def get_trace_id() -> str:
    return _TRACE_ID.get()


def set_trace_id(value: Optional[str]) -> str:
    """Поставить идентификатор в текущий контекст. Неподходящее значение → новый.

    Подходящее: ASCII-токен до 64 символов (буквы, цифры, `._:-`). Всё остальное (пустое,
    кириллица, переносы строк, слишком длинное) заменяем своим — иначе сломается заголовок
    ответа или в журнал попадёт подстановка.
    """
    trace = (value or "").strip()
    if not _TRACE_ALLOWED.match(trace):
        trace = new_trace_id()
    _TRACE_ID.set(trace)
    return trace


def configure_logging_with_trace() -> None:
    """Настроить loguru так, чтобы КАЖДАЯ строка журнала несла trace_id.

    Формат повторяет прежний (время | уровень | модуль:функция:строка — сообщение), добавлено
    поле trace_id после уровня. Идемпотентно: повторный вызов ничего не дублирует.
    """
    global _configured
    if _configured:
        return

    def _patcher(record: dict) -> None:
        record["extra"].setdefault("trace_id", _TRACE_ID.get())

    logger.remove()
    # ВАЖНО: patcher задаётся через configure(), а не через add() — в loguru 0.7.3
    # (наша версия, requirements.txt) у add() такого аргумента нет: TypeError проверен тестом.
    logger.configure(patcher=_patcher)
    logger.add(
        sys.stderr,
        level="DEBUG",
        colorize=True,
        diagnose=False,
        backtrace=False,
        format=("<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | <level>{extra[trace_id]: <12}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                "<level>{message}</level>"),
    )
    _configured = True
    logger.debug("Журналирование с trace_id настроено")


class TraceMiddleware:
    """Проставить trace_id на входе, вернуть его в заголовке, записать метрики запроса.

    Чистый ASGI-middleware (без BaseHTTPMiddleware): не ломает стриминг ответов чата (SSE),
    который у BaseHTTPMiddleware известен проблемами с фоновыми задачами.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = None
        for key, value in (scope.get("headers") or []):
            if key.lower() == TRACE_HEADER.lower().encode():
                incoming = value.decode("latin-1")
                break
        trace = set_trace_id(incoming)
        method = scope.get("method", "?")
        path = scope.get("path", "?")
        started = time.perf_counter()
        status_holder = {"code": 0}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message.get("status", 0)
                # Возвращаем идентификатор клиенту: по нему он назовёт запрос поддержке
                raw = list(message.get("headers") or [])
                raw.append((TRACE_HEADER.lower().encode(), trace.encode()))
                message = {**message, "headers": raw}
            await send(message)

        logger.info(f"{method} {path} начат")
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            took = (time.perf_counter() - started) * 1000
            logger.info(f"{method} {path} → {status_holder['code']} за {took:.0f} мс")
            try:
                from src.monitoring.prometheus import record_http_request
                record_http_request(method, path, status_holder["code"], took / 1000)
            except Exception:
                pass
