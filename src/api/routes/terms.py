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


def _invalidate_glossary() -> None:
    """Правка словаря обязана сразу попасть в задание извлечения (цикл Learn)."""
    try:
        from src.api.services.glossary import invalidate

        invalidate()
    except Exception:  # noqa: BLE001 - сбой кеша не должен валить сохранение
        logger.debug("словарь: кеш глоссария не сброшен")


@router.get("", summary="Словарь терминов: список пар")
async def terms_list(
    entity_type: str = "",
    domain: str = "",
    status: str = Query("", description="pending | approved | rejected | ''"),
    q: str = "",
    hint: str = Query("", description="подсказка автопилота: abbreviation_mismatch | авто-правило"),
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
    # Подсказка автопилота: что система сделала бы сама и на что смотреть.
    # Считается в памяти (без БД): чистая функция от двух строк.
    from src.api.services.automation import advice

    for it in items:
        it["hint"] = advice(it.get("alias", ""), it.get("canonical_name", ""),
                            it.get("entity_type", ""))
    if hint:
        items = [it for it in items if it["hint"].get("hint") == hint]
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
            # Разбивка ожидающих по подсказке: сколько из них машина считает мусором
            # («расшифровка не выводится») и сколько решается правилом. Нужна, чтобы
            # человек видел объём работы пачкой, а не пересчитывал вручную.
            rows = (session.query(EntityAlias.alias, EntityAlias.canonical_name,
                                  EntityAlias.entity_type)
                    .filter(EntityAlias.reviewed.is_(False)).limit(5000).all())
            return {"total": total, "pending": pending, "approved": approved,
                    "rejected": rejected, "by_type": by_type, "_pending_rows": rows}
        finally:
            session.close()

    data = await asyncio.to_thread(_load)
    rows = data.pop("_pending_rows", [])
    from src.api.services.automation import advice

    by_hint: dict = {}
    for alias, canonical, etype in rows:
        h = advice(alias or "", canonical or "", etype or "").get("hint") or "none"
        by_hint[h] = by_hint.get(h, 0) + 1
    return {"status": "ok", "pending_by_hint": by_hint, **data}


class BulkReviewBody(BaseModel):
    ids: List[str] = Field(..., min_length=1, max_length=500,
                           description="id пар, решение применяется ко всем")
    verdict: str = Field(..., pattern="^(approved|rejected)$")
    comment: str = ""


@router.post("/review-bulk", summary="Словарь терминов: подтвердить или отклонить пачкой")
async def terms_review_bulk(body: BulkReviewBody, user: User = Depends(get_current_admin)):
    """Пакетное решение по парам.

    Нужно потому, что очередь мусора (чужие словари, неверные расшифровки) приходит
    десятками: вычитывать их по одной — та самая ручная работа, от которой уходим.
    Решение человека здесь настоящее: каждая пара получает verdict, reviewer, дату,
    и все они попадают в глоссарий извлечения (или в запреты на слияние).
    """
    def _apply():
        session = get_session_local()()
        try:
            changed = (session.query(EntityAlias)
                       .filter(EntityAlias.id.in_(body.ids))
                       .update({EntityAlias.verdict: body.verdict,
                                EntityAlias.reviewed: True},
                               synchronize_session=False))
            session.commit()
            return int(changed or 0)
        finally:
            session.close()

    changed = await asyncio.to_thread(_apply)
    if changed:
        _invalidate_glossary()
    logger.info("словарь: пачкой %s пар решения «%s» (%s)",
                changed, body.verdict, getattr(user, "email", "") or "")
    return {"status": "ok", "changed": changed, "verdict": body.verdict,
            "message": f"Пар обработано: {changed}"}


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
    _invalidate_glossary()
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
    _invalidate_glossary()
    return {"status": "ok", "item": row, "message": "Сохранено"}


@router.get("/{pair_id}/context", summary="Словарь терминов: контекст употребления")
async def terms_context(pair_id: str, limit: int = 5, _: User = Depends(get_current_admin)):
    """Несколько слов до и после термина — чтобы решение принималось по живому тексту.

    Ищем по самому варианту написания; возвращаем фрагменты, где он действительно есть,
    с разметкой **термин** (страница выделяет жирным).
    """
    def _load():
        session = get_session_local()()
        try:
            row = session.query(EntityAlias).filter(EntityAlias.id == pair_id).first()
            return row.alias if row else None
        finally:
            session.close()

    alias = await asyncio.to_thread(_load)
    if not alias:
        raise HTTPException(status_code=404, detail="пара не найдена")
    from src.indexing.term_context import find_context

    items = await find_context(alias, limit=limit)
    return {"status": "ok", "term": alias, "total": len(items), "items": items}


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
    # Решение человека вкатывается в задание извлечения со следующего документа:
    # сбрасываем кеш глоссария, иначе правка подействует только через 10 минут.
    _invalidate_glossary()
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
    _invalidate_glossary()
    return {"status": "ok", "message": "Пара удалена"}
