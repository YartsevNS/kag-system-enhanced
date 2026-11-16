"""Маршруты эталона разметки связей (только администратор)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.middleware.auth_v2 import get_current_admin
from src.database.user_models import User

router = APIRouter()


class PairUpdate(BaseModel):
    clause: str = ""
    measure: str = ""
    note: str = ""


@router.get("", summary="Эталон: список пар на разметку")
async def eval_pairs(limit: int = 20, offset: int = 0,
                     _: User = Depends(get_current_admin)):
    import asyncio

    from src.api.services import eval_service

    items, total = await asyncio.to_thread(eval_service.list_pairs, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/{pair_id}", summary="Эталон: сохранить ответ человека на пару")
async def eval_update(pair_id: int, body: PairUpdate,
                      _: User = Depends(get_current_admin)):
    import asyncio

    from src.api.services import eval_service

    res = await asyncio.to_thread(eval_service.update_pair, pair_id,
                                  body.clause, body.measure, body.note)
    if res.get("status") != "ok":
        raise HTTPException(status_code=404, detail=res.get("detail", "ошибка"))
    return res


@router.get("/stats/all", summary="Эталон: сколько размечено и точность")
async def eval_stats(_: User = Depends(get_current_admin)):
    import asyncio

    from src.api.services import eval_service

    return await asyncio.to_thread(eval_service.stats)
