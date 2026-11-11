"""Словарь терминов и синонимов: просмотр, подтверждение человеком, добавление пар.

Работает напрямую с таблицей `entity_aliases` (Postgres), без Neo4j: словарь нужен
и тогда, когда граф выключен. Пара «вариант → канон» попадает в дело только после
подтверждения человеком (`reviewed`, `verdict='approved'`) — до этого она лишь
предложение.
"""
import asyncio
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func

from src.api.middleware.auth_v2 import get_current_admin
from src.database.entity_alias_models import EntityAlias
from src.database.session import get_session_local
from src.database.user_models import User

logger = logging.getLogger(__name__)

router = APIRouter()


class PairBody(BaseModel):
    alias: str = Field(..., description="вариант написания (как встречается в тексте)")
    canonical: str = Field("", description="канонический термин; пусто = вариант уже канон")
    entity_type: str = Field("term", description="term | organization | document_ref")
    domain: str = Field("infosec", description="infosec | legal | technical | universal")
    comment: str = ""


class ReviewBody(BaseModel):
    verdict: str = Field(..., pattern="^(approved|rejected)$")
    comment: str = ""


def _dict(row: EntityAlias) -> dict:
    return row.to_dict()


@router.get("", summary="Словарь терминов: список пар")
async def terms_list(
    entity_type: str = "",
    domain: str = "",
    status: str = Query("", description="pending | approved | rejected | ''"),
    q: str = "",
    limit: int = 2000,
    _: User = Depends(get_current_admin),
):
    def _load():
        session = get_session_local()()
        try:
            query = session.query(EntityAlias)
            if entity_type:
                query = query.filter(EntityAlias.entity_type == entity_type)
            if domain:
                query = query.filter(EntityAlias.domain == domain)
            if status == "pending":
                query = query.filter(EntityAlias.reviewed.is_(False))
            elif status == "approved":
                query = query.filter(EntityAlias.verdict == "approved")
            elif status == "rejected":
                query = query.filter(EntityAlias.verdict == "rejected")
            if q:
                like = f"%{q.strip()}%"
                query = query.filter((EntityAlias.alias.ilike(like))
                                     | (EntityAlias.canonical_name.ilike(like)))
            rows = query.order_by(EntityAlias.canonical_name, EntityAlias.alias).limit(limit).all()
            return [_dict(r) for r in rows]
        finally:
            session.close()

    items = await asyncio.to_thread(_load)
    return {"status": "ok", "total": len(items), "items": items}


@router.get("/stats", summary="Словарь терминов: счётчики")
async def terms_stats(_: User = Depends(get_current_admin)):
    def _load():
        session = get_session_local()()
        try:
            total = session.query(func.count(EntityAlias.id)).scalar() or 0
            pending = (session.query(func.count(EntityAlias.id))
                       .filter(EntityAlias.reviewed.is_(False)).scalar() or 0)
            approved = (session.query(func.count(EntityAlias.id))
                        .filter(EntityAlias.verdict == "approved").scalar() or 0)
            rejected = (session.query(func.count(EntityAlias.id))
                        .filter(EntityAlias.verdict == "rejected").scalar() or 0)
            by_type = dict(session.query(EntityAlias.entity_type, func.count(EntityAlias.id))
                           .group_by(EntityAlias.entity_type).all())
            return {"total": total, "pending": pending, "approved": approved,
                    "rejected": rejected, "by_type": by_type}
        finally:
            session.close()

    return {"status": "ok", **(await asyncio.to_thread(_load))}


@router.post("", summary="Словарь терминов: добавить пару")
async def terms_add(body: PairBody, user: User = Depends(get_current_admin)):
    alias = (body.alias or "").strip()
    canonical = (body.canonical or "").strip() or alias
    if not alias:
        raise HTTPException(status_code=400, detail="нужен вариант написания")

    def _add():
        session = get_session_local()()
        try:
            exists = (session.query(EntityAlias)
                      .filter(EntityAlias.alias == alias,
                              EntityAlias.canonical_name == canonical,
                              EntityAlias.entity_type == body.entity_type)
                      .first())
            if exists:
                return None
            row = EntityAlias(
                id=str(uuid.uuid4()),
                alias=alias,
                canonical_name=canonical,
                entity_type=body.entity_type or "term",
                domain=body.domain or "infosec",
                source="manual",
                comment=body.comment or "",
                reviewed=True,
                verdict="approved",
            )
            session.add(row)
            session.commit()
            return _dict(row)
        finally:
            session.close()

    row = await asyncio.to_thread(_add)
    if row is None:
        return {"status": "error", "message": "такая пара уже есть"}
    logger.info("словарь: добавлена пара «%s» → «%s» (%s)", alias, canonical,
                getattr(user, "email", "") or "")
    return {"status": "ok", "item": row, "message": "Пара добавлена и подтверждена"}


@router.put("/{pair_id}", summary="Словарь терминов: изменить пару")
async def terms_update(pair_id: str, body: PairBody, _: User = Depends(get_current_admin)):
    def _upd():
        session = get_session_local()()
        try:
            row = session.query(EntityAlias).filter(EntityAlias.id == pair_id).first()
            if not row:
                return None
            if body.alias.strip():
                row.alias = body.alias.strip()
            if body.canonical.strip():
                row.canonical_name = body.canonical.strip()
            if body.entity_type:
                row.entity_type = body.entity_type
            if body.domain:
                row.domain = body.domain
            row.comment = body.comment or row.comment
            session.commit()
            return _dict(row)
        finally:
            session.close()

    row = await asyncio.to_thread(_upd)
    if row is None:
        raise HTTPException(status_code=404, detail="пара не найдена")
    return {"status": "ok", "item": row, "message": "Сохранено"}


@router.post("/{pair_id}/review", summary="Словарь терминов: подтвердить или отклонить")
async def terms_review(pair_id: str, body: ReviewBody, user: User = Depends(get_current_admin)):
    def _review():
        session = get_session_local()()
        try:
            row = session.query(EntityAlias).filter(EntityAlias.id == pair_id).first()
            if not row:
                return None
            row.verdict = body.verdict
            row.reviewed = True
            if body.comment:
                row.comment = body.comment
            session.commit()
            return _dict(row)
        finally:
            session.close()

    row = await asyncio.to_thread(_review)
    if row is None:
        raise HTTPException(status_code=404, detail="пара не найдена")
    logger.info("словарь: «%s» → «%s» %s (%s)", row["alias"], row["canonical_name"],
                body.verdict, getattr(user, "email", "") or "")
    return {"status": "ok", "item": row,
            "message": "Пара подтверждена" if body.verdict == "approved" else "Пара отклонена"}


@router.delete("/{pair_id}", summary="Словарь терминов: удалить пару")
async def terms_delete(pair_id: str, _: User = Depends(get_current_admin)):
    def _del():
        session = get_session_local()()
        try:
            row = session.query(EntityAlias).filter(EntityAlias.id == pair_id).first()
            if not row:
                return False
            session.delete(row)
            session.commit()
            return True
        finally:
            session.close()

    ok = await asyncio.to_thread(_del)
    if not ok:
        raise HTTPException(status_code=404, detail="пара не найдена")
    return {"status": "ok", "message": "Пара удалена"}
