"""
API для логов обработки документов.
"""

from fastapi import APIRouter
from loguru import logger

router = APIRouter()


@router.get("/{document_id}", summary="Лог обработки документа")
async def get_log(document_id: str):
    """Детальный лог всех этапов обработки документа."""
    from src.indexing.process_logger import get_process_log
    log = get_process_log(document_id)
    return {"document_id": document_id, "log": log, "steps": len(log)}


@router.get("", summary="Все логи обработки")
async def list_logs(limit: int = 50):
    """Список всех логов обработки."""
    from src.indexing.process_logger import get_all_process_logs
    return {"logs": get_all_process_logs(limit)}
