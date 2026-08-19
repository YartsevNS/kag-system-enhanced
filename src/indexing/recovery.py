"""
Recovery-модуль: автоматическое восстановление зависших документов.

Проблема: если worker-контейнер перезапускается во время обработки,
документ остаётся в статусе "processing" навсегда.

Решение:
1. При старте worker'а — сканируем БД, сбрасываем зависшие документы
2. Периодическая задача (каждые 5 минут) — фоновый мониторинг
"""

from datetime import datetime, timedelta, timezone
from loguru import logger

# Порог «зависшего» документа. Был 5 мин — recovery сбрасывал в pending любые
# большие документы, которые обрабатываются дольше 5 минут (например, 5000+
# чанков), и они попадали в бесконечный цикл (сброс → рекью → снова сброс),
# блокируя solo-пул и «замораживая» счётчик completed. 60 минут — запас под
# самые большие документы (task_time_limit всё равно 2 часа).
STUCK_THRESHOLD_MINUTES = 60


def recover_stuck_documents(requeue: bool = True, requeue_pending: bool = False) -> dict:
    """
    Сканирует БД на предмет зависших документов и восстанавливает их.

    Args:
        requeue: ставить ли задачи в очередь для зависших processing/delayed
        requeue_pending: ставить ли в очередь документы со статусом pending.
            True — только при старте worker'а (on_worker_ready), чтобы
            подхватить задачи, потерянные при остановке. False — при тиках
            Beat, чтобы не плодить дубли (каждый тик добавлял бы по задаче
            на каждый pending документ).

    Returns:
        dict: {recovered: N, skipped: M, errors: [...]}
    """
    from src.api.services.document_repository import get_doc_repo

    result = {"recovered": 0, "skipped": 0, "errors": [], "details": []}
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=STUCK_THRESHOLD_MINUTES)

    try:
        all_docs = get_doc_repo().get_all() or {}
    except Exception as e:
        logger.error(f"[Recovery] Ошибка чтения документов из БД: {e}")
        result["errors"].append(str(e))
        return result

    for doc_id, doc_data in all_docs.items():
        if not isinstance(doc_data, dict):
            continue

        status = doc_data.get("status", "")
        if status == "delayed":
            # Проверяем — пора ли обработать снова?
            delayed_until_str = doc_data.get("delayed_until")
            if delayed_until_str:
                try:
                    delayed_until = datetime.fromisoformat(delayed_until_str)
                    if delayed_until.tzinfo is None:
                        delayed_until = delayed_until.replace(tzinfo=timezone.utc)
                    if delayed_until > now:
                        continue  # Ещё не время
                except (ValueError, TypeError):
                    pass
            # Время пришло — сбрасываем в pending
            doc_data["status"] = "pending"
            doc_data["progress"] = 0
            doc_data.pop("delayed_until", None)
            get_doc_repo().upsert(doc_id, doc_data)
            result["recovered"] += 1
            if requeue:
                try:
                    # QueueGuard: постановка через enqueue_document — замок не
                    # даст создать дубль, если задача для документа уже стоит.
                    from src.indexing.queue_guard import enqueue_document
                    enqueue_document(doc_id)
                except Exception as e:
                    logger.error(f"[Recovery] Ошибка рекью delayed {doc_id}: {e}")
            continue

        if status not in ("processing", "delayed", "pending"):
            continue

        # Pending: ставим в очередь ТОЛЬКО при старте worker'а (requeue_pending).
        # Задача для pending документа обычно УЖЕ в очереди (её кладёт upload.py
        # при загрузке), а при потере воркера acks_late/reject_on_worker_lost
        # вернёт её в очередь автоматически. Повторный process_document.delay()
        # на каждом тике Beat (5 мин) — причина лавины дублей: очередь
        # разрослась до тысяч копий на одни и те же документы, и worker молотил
        # дубли вместо реальных задач (pending вечно не обрабатывались).
        if status == "pending":
            if requeue_pending:
                doc_data["updated_at"] = now.isoformat()
                get_doc_repo().upsert(doc_id, doc_data)
                result["recovered"] += 1
                try:
                    # QueueGuard: при старте worker'а подхватываем pending,
                    # но только если для документа ещё нет задачи (замок
                    # свободен). Это исключает лавину дублей при рестартах.
                    from src.indexing.queue_guard import enqueue_document
                    enqueue_document(doc_id)
                except Exception as e:
                    logger.error(f"[Recovery] Ошибка рекью pending {doc_id}: {e}")
            continue

        # Проверяем время последнего обновления
        updated_at_str = doc_data.get("updated_at")
        if updated_at_str:
            try:
                if isinstance(updated_at_str, str):
                    updated_at = datetime.fromisoformat(updated_at_str)
                else:
                    updated_at = updated_at_str
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                updated_at = threshold - timedelta(seconds=1)
        else:
            updated_at = threshold - timedelta(seconds=1)

        if updated_at >= threshold:
            result["skipped"] += 1
            continue

        # Документ завис — восстанавливаем
        logger.warning(
            f"[Recovery] ЗАВИСШИЙ документ: {doc_id} "
            f"({doc_data.get('filename')}), "
            f"прошло >{STUCK_THRESHOLD_MINUTES} мин"
        )

        try:
            doc_data["status"] = "pending"
            doc_data["progress"] = 0
            doc_data["error"] = (
                f"Автовосстановление {now.isoformat()}: "
                f"задача потеряна при перезапуске worker'а"
            )
            doc_data["updated_at"] = now.isoformat()
            get_doc_repo().upsert(doc_id, doc_data)
            result["recovered"] += 1
            result["details"].append({
                "document_id": doc_id,
                "filename": doc_data.get("filename", "?"),
                "was_stuck_since": updated_at_str,
            })

            # QueueGuard: снимаем замок ПЕРЕД перезапуском. Если задача реально
            # потеряна (worker убит/перезапущен), release_lock в process_document
            # не выполнился, и замок висит до TTL (6 часов) — recovery не смог
            # бы перезапустить документ (enqueue вернул бы False). Раз документ
            # завис дольше STUCK_THRESHOLD, задача точно мертва — замок можно
            # снять. Если же задача каким-то образом ещё жива, дубль не создастся:
            # process_document (QueueGuard уровень 3) пропустит обработку при
            # status=processing.
            from src.indexing.queue_guard import release_lock
            release_lock(doc_id)

            if requeue:
                try:
                    # QueueGuard: постановка через enqueue_document — замок не
                    # даст создать дубль, если задача для документа уже стоит.
                    from src.indexing.queue_guard import enqueue_document
                    task = enqueue_document(doc_id)
                    logger.info(
                        f"[Recovery] Перезапущен {doc_id} -> enqueue={task}"
                    )
                except Exception as e:
                    logger.error(f"[Recovery] Ошибка рекью {doc_id}: {e}")
                    result["errors"].append(f"requeue {doc_id}: {e}")

        except Exception as e:
            logger.error(f"[Recovery] Ошибка восстановления {doc_id}: {e}")
            result["errors"].append(str(e))

    if result["recovered"] > 0:
        logger.warning(
            f"[Recovery] ИТОГО: восстановлено {result['recovered']}, "
            f"активных {result['skipped']}"
        )

    return result
