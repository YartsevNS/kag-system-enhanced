"""Маршруты страницы «Конвейер обработки»: этапы, время, включение сбора."""

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from src.api.middleware.auth_v2 import get_current_admin
from src.database.user_models import User

logger = logging.getLogger(__name__)
router = APIRouter()


class CollectBody(BaseModel):
    enabled: bool = Field(..., description="true — собирать статистику этапов, false — не писать")


@router.get("/documents", summary="Документы с логом этапов обработки")
async def pipeline_documents(limit: int = 30, _: User = Depends(get_current_admin)):
    from src.api.services import pipeline

    return {"ok": True, "collecting": await asyncio.to_thread(pipeline.enabled),
            "items": await asyncio.to_thread(pipeline.documents, limit)}


@router.get("/{document_id}", summary="Этапы и время обработки одного документа")
async def pipeline_detail(document_id: str, _: User = Depends(get_current_admin)):
    from src.api.services import pipeline

    return {"ok": True, **await asyncio.to_thread(pipeline.detail, document_id)}


@router.post("/collect", summary="Включить или выключить сбор статистики этапов")
async def pipeline_collect(body: CollectBody, user: User = Depends(get_current_admin)):
    from src.api.services import pipeline

    ok = await asyncio.to_thread(pipeline.set_enabled, body.enabled,
                                 getattr(user, "email", "") or "")
    return {"ok": bool(ok), "collecting": body.enabled,
            "message": "Сбор статистики включён" if body.enabled else "Сбор статистики выключен"}
