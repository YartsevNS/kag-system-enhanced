"""Тесты предохранителей: проверка записи векторов, liveness-recovery, ретраи Neo4j.

Все три механизма появились после разбора инцидентов 2026-09-12:
  - документ «completed» при 0 точек в Qdrant (коллизии point_id);
  - документ висел в processing 60 минут после потери задачи (worker перезапущен);
  - пачка связей в графе молча терялась на TransientError.DeadlockDetected.
"""

from src.indexing.indexing_guards import (
    POINTS_EMPTY,
    POINTS_MISMATCH,
    POINTS_OK,
    check_points_written,
    is_transient_neo4j_error,
    points_verdict_message,
    recovery_reason,
    run_with_transient_retry,
)


# ── проверка записи векторов ────────────────────────────────────────────────

def test_points_ok_when_counts_match():
    assert check_points_written(120, 120) == POINTS_OK


def test_points_empty_is_failure():
    """Ровно тот инцидент: чанки есть, точек нет — индексация не сохранилась."""
    assert check_points_written(180, 0) == POINTS_EMPTY
    assert "0 точек" in points_verdict_message(POINTS_EMPTY, 180, 0)


def test_points_mismatch_flagged():
    assert check_points_written(100, 73) == POINTS_MISMATCH
    assert "73" in points_verdict_message(POINTS_MISMATCH, 100, 73)


def test_empty_document_is_not_a_failure():
    """Документ без текста (0 чанков) — не ошибка индексации."""
    assert check_points_written(0, 0) == POINTS_OK


# ── liveness-recovery ───────────────────────────────────────────────────────

def _reason(status="processing", age=10.0, alive=None, doc_id="d1"):
    return recovery_reason(
        status=status,
        age_minutes=age,
        alive_document_ids=alive,
        document_id=doc_id,
        stuck_threshold_minutes=60,
        liveness_grace_minutes=5,
    )


def test_completed_documents_never_recovered():
    assert _reason(status="completed", age=10_000) is None


def test_live_task_is_not_recovered():
    """Задача в active/reserved — документ обрабатывается, трогать нельзя."""
    assert _reason(age=30, alive={"d1"}) is None


def test_lost_task_recovered_after_grace():
    """Задачи нет у воркеров и прошло больше grace — восстанавливаем сразу."""
    assert _reason(age=6, alive=set()) == "lost_task"
    assert _reason(age=59, alive={"other"}) == "lost_task"


def test_lost_task_not_recovered_inside_grace():
    """В первые минуты задача может ещё не попасть в active — ждём."""
    assert _reason(age=1, alive=set()) is None
    assert _reason(age=4.9, alive=set()) is None


def test_unknown_task_state_falls_back_to_hard_threshold():
    """inspect недоступен (alive=None) — работает только порог 60 минут."""
    assert _reason(age=59, alive=None) is None
    assert _reason(age=61, alive=None) == "stuck_threshold"


def test_hard_threshold_wins_even_if_task_alive():
    """Задача числится живой, но висит больше порога — страховка срабатывает."""
    assert _reason(age=61, alive={"d1"}) == "stuck_threshold"


# ── ретраи транзиентных ошибок Neo4j ────────────────────────────────────────

class _Deadlock(Exception):
    """Имитация Neo4j TransientError.DeadlockDetected."""

    def __init__(self):
        super().__init__(
            "{code: Neo.TransientError.Transaction.DeadlockDetected} "
            "can't acquire ExclusiveLock"
        )


class _OtherError(Exception):
    pass


def test_transient_detection():
    assert is_transient_neo4j_error(_Deadlock())
    assert not is_transient_neo4j_error(_OtherError("syntax error"))


def test_retry_succeeds_after_deadlocks():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Deadlock()
        return "ok"

    assert run_with_transient_retry(flaky, attempts=3, sleep_fn=lambda _s: None) == "ok"
    assert calls["n"] == 3


def test_retry_gives_up_after_attempts():
    calls = {"n": 0}

    def always_deadlock():
        calls["n"] += 1
        raise _Deadlock()

    try:
        run_with_transient_retry(always_deadlock, attempts=3, sleep_fn=lambda _s: None)
        raise AssertionError("должно было пробросить исключение")
    except _Deadlock:
        pass
    assert calls["n"] == 3


def test_non_transient_error_is_not_retried():
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise _OtherError("bad query")

    try:
        run_with_transient_retry(broken, attempts=3, sleep_fn=lambda _s: None)
        raise AssertionError("должно было пробросить исключение")
    except _OtherError:
        pass
    assert calls["n"] == 1


def test_retry_returns_value_without_retries():
    assert run_with_transient_retry(lambda: 42, sleep_fn=lambda _s: None) == 42
