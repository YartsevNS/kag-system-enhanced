"""Галочка «Обработка документов» должна действовать и на очередь.

Юниты на processing_blocked() (fail-open при сбое настроек) + структурные
проверки, что проверка действительно встроена в задачу, в recovery и что снятие
блокировки подхватывает очередь. Структурные проверки нужны потому, что без них
правка «галочка не действует на очередь» легко возвращается: API-часть (423)
выглядит корректно, а задача молча обрабатывает документ.
"""
from pathlib import Path

import pytest

from src.indexing import processing_guard
from src.indexing.processing_guard import DEFAULT_MESSAGE, PROCESSING_BLOCK_RETRY_S, processing_blocked

ROOT = Path(__file__).resolve().parents[1]


class _FakeStore:
    """Подменяет чтение настройки в самом guard-модуле.

    Патчить config_store снаружи бессмысленно: модуль импортируется из двух
    мест по-разному (в тестовой среде их два), и подмена не долетает до кода.
    """

    def __init__(self, value=None, boom=False):
        self.value = value
        self.boom = boom

    def __call__(self):
        if self.boom:
            raise RuntimeError("БД недоступна")
        return self.value


def _patch(monkeypatch, store):
    monkeypatch.setattr(processing_guard, "_load_processing_cfg", store)

    # И одна проверка «как в бою»: настоящий читатель при недоступной БД
    # не должен останавливать очередь (fail-open).


def test_blocked_with_message(monkeypatch):
    _patch(monkeypatch, _FakeStore({"blocked": True, "message": "тех. работы до 18:00"}))
    assert processing_blocked() == (True, "тех. работы до 18:00")


def test_blocked_without_message_uses_default(monkeypatch):
    _patch(monkeypatch, _FakeStore({"blocked": True}))
    assert processing_blocked() == (True, DEFAULT_MESSAGE)


def test_blocked_empty_message_uses_default(monkeypatch):
    _patch(monkeypatch, _FakeStore({"blocked": True, "message": "   "}))
    assert processing_blocked() == (True, DEFAULT_MESSAGE)


def test_not_blocked(monkeypatch):
    _patch(monkeypatch, _FakeStore({"blocked": False, "message": "старое"}))
    assert processing_blocked() == (False, "")


def test_missing_or_odd_config_is_not_blocked(monkeypatch):
    for value in (None, {}, "строка вместо словаря", []):
        _patch(monkeypatch, _FakeStore(value))
        assert processing_blocked() == (False, ""), f"значение {value!r} не должно блокировать"


def test_read_failure_is_fail_open(monkeypatch):
    """Сбой чтения настроек не должен останавливать очередь."""
    _patch(monkeypatch, _FakeStore(boom=True))
    assert processing_blocked() == (False, "")


def test_retry_period_is_reasonable():
    # Меньше 30 с — очередь будет молотить проверками; больше 10 мин —
    # документы после снятия блокировки ждут слишком долго.
    assert 30 <= PROCESSING_BLOCK_RETRY_S <= 600


def test_task_defers_when_blocked():
    src = (ROOT / "src/indexing/tasks.py").read_text(encoding="utf-8")
    start = src.index("def process_document(")
    block = src[start:start + 9000]
    assert "processing_blocked()" in block, "задача обязана проверять блокировку"
    assert "PROCESSING_BLOCK_RETRY_S" in block, "отсрочка должна быть общей константой"
    # Откладываем, а не теряем: retry без ограничения попыток.
    assert "self.retry(countdown=PROCESSING_BLOCK_RETRY_S, max_retries=None)" in block, (
        "при блокировке задачу нужно отложить (retry), а не завершать"
    )
    # Проверка стоит ДО старта обработки.
    assert block.index("processing_blocked()") < block.index("document_service.process_document")


def test_recovery_does_not_requeue_when_blocked():
    src = (ROOT / "src/indexing/recovery.py").read_text(encoding="utf-8")
    start = src.index("def recover_stuck_documents(")
    block = src[start:start + 2000]
    assert "processing_blocked()" in block, "recovery тоже обязан уважать паузу"
    assert "requeue = False" in block and "requeue_pending = False" in block


def test_unblocking_resumes_queue():
    src = (ROOT / "src/api/routes/admin_models.py").read_text(encoding="utf-8")
    start = src.index("async def save_processing_config(")
    block = src[start:start + 2500]
    assert "recover_stuck_documents" in block, "снятие блокировки должно подхватывать очередь"
    assert "asyncio.to_thread" in block, "recovery делает блокирующие вызовы — только в потоке"
    assert 'blocked_now' in block, "ответ должен возвращать новое состояние"
