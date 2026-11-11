"""Очередь возможных дублей документов: список кандидатов и решение человека.

Точный дубль (тот же SHA-256) отсекается на загрузке и сюда не попадает: здесь только
случаи «тот же документ, другие байты» — по обозначению (издатель+вид+номер) или по
названию. Без решения человека записи не сливаются, версии не создаются.
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.middleware.auth_v2 import get_current_admin
from src.database.session import get_session_local
from src.database.user_models import User

logger = logging.getLogger(__name__)

router = APIRouter()


class DecideBody(BaseModel):
    action: str = Field(..., description="new_version | copy | distinct")
    comment: str = ""


def _who(user: Optional[User]) -> str:
    if user is None:
        return ""
    return getattr(user, "email", "") or getattr(user, "username", "") or getattr(user, "id", "") or ""


@router.get("", summary="Возможные дубли: список")
async def duplicates_list(status: str = "pending", limit: int = 100,
                          _: User = Depends(get_current_admin)):
    from src.api.services import duplicates as dups

    db = get_session_local()()
    try:
        items = await asyncio.to_thread(dups.list_candidates, db, status, limit)
    finally:
        db.close()
    return {"status": "ok", "total": len(items), "items": items, "filter": status}


@router.post("/{candidate_id}/decide", summary="Решение по кандидату в дубли")
async def duplicates_decide(candidate_id: str, body: DecideBody,
                            user: User = Depends(get_current_admin)):
    from src.api.services import duplicates as dups

    db = get_session_local()()
    try:
        res = await asyncio.to_thread(dups.decide, db, candidate_id, body.action, _who(user))
    finally:
        db.close()
    if res.get("status") != "ok":
        raise HTTPException(status_code=400, detail=res.get("message") or "решение не принято")

    # Копия: источник дописан к существующему, новую запись убираем вместе с векторами.
    if body.action == "copy" and res.get("delete"):
        try:
            from src.api.services.document_service import document_service

            ok = await document_service.delete_document(res["delete"])
            res["deleted"] = bool(ok)
        except Exception as exc:  # noqa: BLE001
            logger.warning("дубли: удаление копии не удалось: %s", str(exc)[:160])
            res["deleted"] = False
            res["message"] = (res.get("message", "") +
                              " Внимание: запись-копия не удалена, удалите её вручную на странице «Документы».")
    return res
