"""
QueueGuard — центральная защита от дублирования задач обработки документов.

Зачем нужен:
Документ может ставиться в очередь из разных мест (upload, recovery, reindex,
reprocess, batch). Раньше каждое место вызывало process_document.delay()
напрямую, без общего контроля — в результате на один документ накапливались
сотни одинаковых задач, worker обрабатывал документ по несколько раз, а
статусы прыгали pending ↔ processing ↔ completed (наблюдалось 22978 дублей
в очереди на 26 документов).

Механизм защиты (три уровня, чтобы дубль был невозможен в любом сценарии):

1. Redis-замок (SET NX + TTL). enqueue_document() атомарно занимает ключ
   qguard:{doc_id}. Повторный вызов для того же документа возвращает False —
   задача НЕ создаётся. TTL (6 часов) > task_time_limit (2 часа): замок
   гарантированно живёт дольше самой долгой задачи, но не вечно — если
   worker убит жёстко (задача не успела снять замок), через 6 часов документ
   снова можно поставить.

2. Проверка статуса в БД. enqueue_document() не ставит completed-документ
   повторно без force=True. Принудительная переобработка (reindex-all,
   reprocess) явно передаёт force=True.

3. Проверка в самой задаче process_document(). Если задача-дубль всё же
   попала в очередь (например, осталась от старого кода) и документ уже
   processing (другая копия выполняется) или completed (уже обработан) —
   задача выходит без обработки.

Использование: ВСЕ места, которые ставят документ на обработку, вызывают
enqueue_document() вместо process_document.delay().
"""

import time
from loguru import logger

try:
    import redis as redis_lib
except ImportError:  # pragma: no cover
    redis_lib = None

# TTL замка: 6 часов. Celery task_time_limit = 2 часа, поэтому замок всегда
# живёт дольше задачи, но не блокирует документ навсегда при потере воркера.
LOCK_TTL_SECONDS = 6 * 3600


def _redis():
    """Подключение к Redis (db 1 = тот же, что Celery broker)."""
    from src.config import get_settings
    s = get_settings()
    return redis_lib.Redis(
        host=s.REDIS_HOST,
        port=s.REDIS_PORT,
        db=1,
        password=s.REDIS_PASSWORD or None,
        decode_responses=True,
    )


def _key(document_id: str) -> str:
    return f"qguard:{document_id}"


def enqueue_document(document_id: str, force: bool = False) -> bool:
    """
    Поставить документ в очередь обработки ровно один раз.

    Args:
        document_id: id документа.
        force: True — разрешить повторную постановку completed-документа
               (reindex-all, reprocess). По умолчанию False: уже обработанный
               документ не переставляется (защита от случайных дублей).

    Returns:
        True — задача поставлена; False — задача не создана
        (уже стоит в очереди/исполняется, уже completed, либо ошибка).
    """
    if redis_lib is None:  # pragma: no cover
        logger.error("[QueueGuard] redis не установлен — ставлю задачу напрямую")
        from src.indexing.tasks import process_document
        process_document.delay(document_id)
        return True

    r = _redis()
    key = _key(document_id)

    # ── Уровень 1: атомарный замок ────────────────────────────────────────
    # SET NX: если ключ уже существует — для документа уже есть задача
    # в очереди или она выполняется. Дубль не создаём.
    locked = r.set(key, "1", nx=True, ex=LOCK_TTL_SECONDS)
    if not locked:
        logger.debug(
            f"[QueueGuard] {document_id}: задача уже в очереди/исполнении — пропуск"
        )
        return False

    # ── Уровень 2: проверка статуса в БД ─────────────────────────────────
    # completed без force не переставляем (защита от дублей reindex/recovery).
    if not force:
        try:
            from src.api.services.document_repository import get_doc_repo
            doc = get_doc_repo().get_dict(document_id)
            if doc and isinstance(doc, dict) and doc.get("status") == "completed":
                r.delete(key)
                logger.debug(
                    f"[QueueGuard] {document_id}: уже completed — пропуск"
                )
                return False
        except Exception as e:
            logger.warning(
                f"[QueueGuard] {document_id}: не смог проверить статус ({e}) — продолжаю"
            )

    # ── Постановка задачи ────────────────────────────────────────────────
    # Ленивый импорт: tasks.py импортирует document_service, а тот — много
    # всего; здесь это не нужно до реальной постановки. К тому же избегаем
    # циклического импорта (tasks → queue_guard → tasks).
    try:
        from src.indexing.tasks import process_document
        # force пробрасывается в задачу: process_document(force=True) разрешает
        # обработку completed-документа (reindex/reprocess). Без этого задача
        # получила бы force=False и пропустила бы документ (уровень 3).
        task = process_document.delay(document_id, force=force)
        logger.info(
            f"[QueueGuard] {document_id}: поставлен (task {task.id}, force={force})"
        )
        return True
    except Exception as e:
        logger.error(
            f"[QueueGuard] {document_id}: ошибка постановки ({e}) — снимаю замок"
        )
        try:
            r.delete(key)
        except Exception:
            pass
        return False


def release_lock(document_id: str) -> None:
    """
    Снять замок после завершения задачи.

    Вызывается из process_document при успехе, при delayed (провайдер
    недоступен) и при финальной ошибке (исчерпаны retry). При retry замок
    НЕ снимается — задача будет перепоставлена Celery, и замок защищает от
    дубля, пока идёт цепочка retry.
    """
    if redis_lib is None:
        return
    try:
        r = _redis()
        r.delete(_key(document_id))
    except Exception:
        pass
