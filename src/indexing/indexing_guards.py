"""Проверки-предохранители для индексации и графа.

Зачем отдельный модуль: проверки должны быть чистыми функциями без побочных
эффектов, чтобы их можно было покрыть юнит-тестами (у нас Qdrant/Neo4j в
тестах недоступны). Каждая проверка отвечает на один вопрос и возвращает
вердикт, а решение (падать, писать warning) принимает вызывающий код.

История: 2026-09-12 документ отмечался completed по числу чанков из парсера,
хотя в Qdrant по нему было 0 точек (коллизии point_id перезаписывали данные).
Ошибка была НЕВИДИМА — статус «готово», поиск пуст. Проверка ниже закрывает
этот класс ошибок: 0 точек после векторзации = провал индексации.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

# Вердикты проверки записи векторов
POINTS_OK = "ok"
POINTS_EMPTY = "empty"          # ни одной точки — индексация не сохранилась
POINTS_MISMATCH = "mismatch"    # точек меньше/больше, чем чанков


def check_points_written(expected_chunks: int, actual_points: int) -> str:
    """Сверить число чанков с числом точек документа в Qdrant.

    Пустой документ (0 чанков) — не ошибка: у документа может не быть текста.
    А вот 0 точек при ненулевом числе чанков — провал: документ «обработан»,
    но его в поиске нет.
    """
    if expected_chunks <= 0:
        return POINTS_OK
    if actual_points <= 0:
        return POINTS_EMPTY
    if actual_points != expected_chunks:
        return POINTS_MISMATCH
    return POINTS_OK


def points_verdict_message(verdict: str, expected: int, actual: int) -> str:
    """Человекочитаемое пояснение вердикта (для логов и process-лога)."""
    if verdict == POINTS_EMPTY:
        return (f"в Qdrant 0 точек при ожидаемых {expected} чанках — "
                f"индексация не сохранилась")
    if verdict == POINTS_MISMATCH:
        return f"точек в Qdrant {actual}, а чанков {expected} — расхождение"
    return f"точек в Qdrant {actual} — совпадает с числом чанков {expected}"


def recovery_reason(
    status: str,
    age_minutes: float,
    alive_document_ids: Optional[set],
    document_id: str,
    stuck_threshold_minutes: float,
    liveness_grace_minutes: float,
) -> Optional[str]:
    """Пора ли восстанавливать документ и почему (None — не пора).

    Зачем liveness: раньше recovery ждал фиксированные 60 минут, потому что
    не знал, жива ли задача. Если задачи нет ни в active, ни в reserved —
    она потеряна (рестарт/убийство worker'а), и ждать 60 минут бессмысленно:
    пользователь всё это время видит «обрабатывается». Порог 60 минут остаётся
    как страховка, когда состояние задач узнать не удалось.

    alive_document_ids = None означает «состояние задач неизвестно»
    (inspect недоступен) — тогда работает только жёсткий порог.
    """
    if status != "processing":
        return None

    known = alive_document_ids is not None
    alive = bool(known and document_id in alive_document_ids)

    if age_minutes >= stuck_threshold_minutes:
        return "stuck_threshold"
    if known and not alive and age_minutes >= liveness_grace_minutes:
        return "lost_task"
    return None


def is_transient_neo4j_error(exc: BaseException) -> bool:
    """Транзиентная ли ошибка Neo4j (дедлок и прочие TransientError).

    Такие ошибки проходят при повторе: 2026-09-12 при параллельной записи
    нескольких документов Neo4j отдавал TransientError.DeadlockDetected, а
    batch_create_relations логировал это как warning — пачка связей молча
    терялась (в логе API было 17 таких ошибок).
    """
    name = type(exc).__name__
    text = str(exc)
    return ("Deadlock" in text or "TransientError" in text
            or "Deadlock" in name or "Transient" in name)


def run_with_transient_retry(
    fn: Callable[[], T],
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    """Выполнить fn, повторив её при транзиентных ошибках Neo4j.

    Нетранзиентные ошибки пробрасываются сразу: повторять их бессмысленно, а
    глотать нельзя — именно так терялись связи в графе.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not is_transient_neo4j_error(exc) or attempt == attempts:
                raise
            sleep_fn(base_delay * attempt)
    raise RuntimeError("run_with_transient_retry: unreachable")
