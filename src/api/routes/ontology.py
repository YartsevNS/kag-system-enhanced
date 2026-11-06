"""Маршруты редактора онтологии TypeDB (ADR-004).

Только администратор: правка схемы меняет то, на чём строится доказательная база,
поэтому права те же, что у админских разделов.

Драйвер TypeDB синхронный — каждый вызов уходит в поток (asyncio.to_thread),
иначе запрос схемы на большом типе блокирует event loop api.

Что важно для UI:
- GET /status отвечает всегда (200) и говорит, настроен ли TypeDB: страница должна
  показать «недоступно» с причиной, а не пустой граф.
- validate не меняет схему (SCHEMA-транзакция с откатом); apply — меняет.
"""
import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.middleware.auth_v2 import get_current_admin
from src.api.services.ontology_service import OntologyUnavailable, ontology_service
from src.database.user_models import User
from src.indexing import typedb_facts

router = APIRouter()


class DefineRequest(BaseModel):
    define: str = Field(default="", description="Текст TypeQL: define … / undefine …")


class TypeRequest(BaseModel):
    kind: str = Field(description="entity | relation | attribute")
    label: str
    owns: List[str] = []


@router.get("/status", summary="Доступность TypeDB и параметры подключения")
async def ontology_status(_: User = Depends(get_current_admin)):
    cfg = typedb_facts._cfg()
    return {
        "available": typedb_facts.is_available(),
        "driver_installed": typedb_facts._TYPEDB_IMPORTED,
        "addr": cfg["addr"],
        "db": cfg["db"],
        "user": cfg["user"],
        "password_set": bool(cfg["pass"]),
    }


@router.get("/schema", summary="Прочитать схему онтологии")
async def ontology_schema(_: User = Depends(get_current_admin)):
    try:
        return await asyncio.to_thread(ontology_service.get_schema)
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — наверх уходит одна строка
        raise HTTPException(status_code=502, detail=f"TypeDB: {str(exc).splitlines()[-1][:300]}")


@router.post("/validate", summary="Проверить define без применения")
async def ontology_validate(req: DefineRequest, _: User = Depends(get_current_admin)):
    try:
        return await asyncio.to_thread(ontology_service.validate, req.define)
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/apply", summary="Применить define к схеме")
async def ontology_apply(req: DefineRequest, _: User = Depends(get_current_admin)):
    try:
        return await asyncio.to_thread(ontology_service.apply, req.define)
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/type", summary="Создать тип (entity/relation/attribute)")
async def ontology_create_type(req: TypeRequest, _: User = Depends(get_current_admin)):
    try:
        return await asyncio.to_thread(ontology_service.create_type, req.kind, req.label, req.owns)
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.delete("/type/{kind}/{label}", summary="Удалить тип через undefine")
async def ontology_delete_type(kind: str, label: str, _: User = Depends(get_current_admin)):
    try:
        return await asyncio.to_thread(ontology_service.delete_type, kind, label)
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
