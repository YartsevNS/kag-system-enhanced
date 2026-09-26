"""Сквозной trace_id: заголовок ответа, контекст и попадание в журналы.

Зачем тесты: договорились, что по одному идентификатору можно поднять весь путь запроса.
Значит, идентификатор обязан (1) возвращаться клиенту, (2) приниматься от клиента, (3) стоять
в каждой строке журнала, включая строки из рабочих потоков (asyncio.to_thread копирует контекст).
"""
import asyncio
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from src.api.trace import (TRACE_HEADER, TraceMiddleware, configure_logging_with_trace,
                           get_trace_id, new_trace_id, set_trace_id)


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)

    @app.get("/ping")
    async def ping():
        return {"trace": get_trace_id()}

    return TestClient(app)


def test_trace_id_returned_in_header():
    with _client() as c:
        r = c.get("/ping")
    assert r.status_code == 200
    trace = r.headers.get(TRACE_HEADER)
    assert trace and len(trace) == 12, f"в ответе нет X-Trace-ID: {dict(r.headers)}"
    assert r.json()["trace"] == trace, "в обработчике должен быть тот же идентификатор"


def test_client_trace_id_is_echoed():
    with _client() as c:
        r = c.get("/ping", headers={TRACE_HEADER: "client-42.ok"})
    assert r.headers.get(TRACE_HEADER) == "client-42.ok", "идентификатор клиента нужно сохранять"
    assert r.json()["trace"] == "client-42.ok"


def test_garbage_trace_id_replaced():
    """Невалидные значения (кириллица, переносы строк, слишком длинное) заменяем своим.

    Кириллица в заголовке — не мелочь: HTTP-заголовки кодируются latin-1, и такой идентификатор
    ломает отдачу ответа (UnicodeEncodeError поймал тест). Перенос строки — подстановка в журнал.
    """
    for bad in ("x" * 200, "строка\nс переносом", "   ", "кириллица-42", "с пробелом внутри"):
        result = set_trace_id(bad)
        assert result != bad, f"невалидное значение принято: {bad!r}"
        assert len(result) == 12 and result.isalnum()
    assert len(set_trace_id("valid.trace-1:2")) == len("valid.trace-1:2"), \
        "корректный идентификатор клиента менять нельзя"


def test_new_trace_id_is_unique():
    assert len({new_trace_id() for _ in range(200)}) == 200


def test_trace_id_is_visible_in_logs(monkeypatch):
    """Проверяем сам механизм: запись из журнала несёт trace_id из контекста."""
    configure_logging_with_trace()   # тот же вызов, что делает api на старте
    seen = []
    sink_id = logger.add(lambda msg: seen.append(msg.record), level="INFO")
    try:
        set_trace_id("test-trace-1")
        logger.info("проверка журнала")
    finally:
        logger.remove(sink_id)
    assert seen, "журнал ничего не записал"
    assert seen[-1]["extra"].get("trace_id") == "test-trace-1", \
        f"в записи журнала нет trace_id: {seen[-1]['extra']}"


def test_trace_id_reaches_worker_thread():
    """asyncio.to_thread копирует контекст: строка из потока тоже несёт trace_id."""
    from src.api.trace import new_trace_id as _new
    trace = set_trace_id(_new())
    got = {}

    def in_thread():
        got["trace"] = get_trace_id()
        got["thread"] = threading.current_thread() is threading.main_thread()

    asyncio.run(asyncio.to_thread(in_thread))
    assert got["trace"] == trace, "в потоке потерялся trace_id"
    assert got["thread"] is False, "проверка должна идти в отдельном потоке"
