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

STUCK_THRESHOLD_MINUTES = 5


def recover_stuck_documents(requeue: bool = True) -> dict:
    """
    Сканирует БД на предмет зависших документов и восстанавливает их.

    Returns:
        dict: {recovered: N, skipped: M, errors: [...]}
    """
    from src.api.services.config_store import config_store

    result = {"recovered": 0, "skipped": 0, "errors": [], "details": []}
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=STUCK_THRESHOLD_MINUTES)

    try:
        all_docs = config_store.get_all("documents") or {}
    except Exception as e:
        logger.error(f"[Recovery] Ошибка чтения документов из БД: {e}")
        result["errors"].append(str(e))
        return result

    for doc_id, doc_data in all_docs.items():
        if not isinstance(doc_data, dict):
            continue

        status = doc_data.get("status", "")
        if status != "processing":
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
            config_store.set("documents", doc_id, doc_data)
            result["recovered"] += 1
            result["details"].append({
                "document_id": doc_id,
                "filename": doc_data.get("filename", "?"),
                "was_stuck_since": updated_at_str,
            })

            if requeue:
                try:
                    from src.indexing.tasks import process_document
                    task = process_document.delay(doc_id)
                    logger.info(
                        f"[Recovery] Перезапущен {doc_id} -> task {task.id}"
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
