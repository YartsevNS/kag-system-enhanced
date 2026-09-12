"""Роут страницы «Чанки»: общий список чанков с корректным порядком.

Зачем отдельный роут: страница /chunks при отсутствии выбранного документа
запрашивала `/api/v1/chunks`, которого не существовало. Получив 404, страница
падала в fallback на семантический поиск `/chat/search` с пустым запросом — и
чанки выводились в порядке релевантности (по сути случайном), а не по номерам.

Порядок: документ (по document_id) → номер чанка (chunk_seq из metadata) → id.
Разбор номера — общий, в src/api/services/chunk_order.py.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends

from src.api.middleware.auth_v2 import get_current_user_optional
from src.database.user_models import User

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/chunks", summary="Чанки документов (общий список, по порядку)")
async def list_chunks(
    offset: int = 0,
    limit: int = 10,
    document_id: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Все чанки (или одного документа), отсортированные по документу и номеру чанка.

    Пагинация применяется ПОСЛЕ сортировки — иначе порядок на страницах «поехал» бы.
    """
    from src.api.services.chunk_order import chunk_seq_of
    from src.indexing.qdrant_service import get_qdrant_service

    try:
        qdrant_service = get_qdrant_service()
        scroll_filter = None
        if document_id:
            scroll_filter = {"must": [{"key": "document_id", "match": {"value": document_id}}]}

        points = qdrant_service.scroll_points(filter=scroll_filter, limit=50000)

        items = []
        for p in points:
            payload = p.get("payload") or {}
            items.append({
                "id": p.get("id", ""),
                "chunk_id": payload.get("chunk_id", ""),
                "chunk_seq": chunk_seq_of(payload),
                "text": payload.get("text", payload.get("content", "")),
                "document_id": payload.get("document_id", ""),
                "filename": payload.get("filename", ""),
                "file_type": payload.get("file_type", ""),
                "metadata": payload.get("metadata", {}),
            })

        # документ → номер чанка → id
        items.sort(key=lambda c: (str(c.get("document_id") or ""),
                                  c.get("chunk_seq") or 10 ** 9,
                                  str(c.get("chunk_id") or "")))
        total = len(items)
        page = items[offset:offset + limit]
        return {"chunks": page, "total": total, "offset": offset, "limit": limit}
    except Exception as e:
        logger.error(f"Ошибка получения общего списка чанков: {e}")
        return {"chunks": [], "total": 0, "error": str(e)}
