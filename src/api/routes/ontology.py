"""Маршруты редактора онтологии TypeDB (ADR-004).

Только администратор: правка схемы меняет то, на чём строится доказательная база,
поэтому права те же, что у админских разделов.

Драйвер TypeDB синхронный — каждый вызов уходит в поток (asyncio.to_thread),
иначе запрос схемы на большом типе блокирует event loop api.

Что важно для UI:
- GET /status отвечает всегда (200) и говорит, настроен ли TypeDB: страница должна
  показать «недоступно» с причиной, а не пустой граф.
- validate не меняет схему (SCHEMA-транзакция с откатом); apply — меняет.
- КАЖДОЕ изменение схемы пишется в журнал версий (кто, когда, что) — включая неудачные
  попытки: «пробовал и не вышло» тоже событие, которое ищут при разборе.
- undo/redo работают через версии, а не через клиентский стек: сервер помнит историю,
  поэтому «Отменить» работает и после перезагрузки страницы.
"""
import asyncio
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from src.api.middleware.auth_v2 import get_current_admin
from src.api.services.ontology_service import OntologyUnavailable, ontology_service
from src.api.services.ontology_versions import diff_snapshots, ontology_versions, snapshot_of
from src.database.user_models import User
from src.indexing import typedb_facts

router = APIRouter()


class DefineRequest(BaseModel):
    define: str = Field(default="", description="Текст TypeQL: define … / undefine …")
    comment: str = Field(default="", description="Что и зачем меняем — попадёт в журнал")


class TypeRequest(BaseModel):
    kind: str = Field(description="entity | relation | attribute")
    label: str
    owns: List[str] = []
    roles: List[str] = []
    value: str = "string"
    comment: str = Field(default="", description="Пояснение для журнала версий")


class TemplateRequest(BaseModel):
    text: str = Field(description="Содержимое файла .tql")
    comment: str = ""


class PositionRequest(BaseModel):
    current_version_id: str = Field(default="", description="Где сейчас стоит пользователь в журнале")


def _who(user: User) -> str:
    return (getattr(user, "username", "") or getattr(user, "email", "") or "")[:120]


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
async def ontology_apply(req: DefineRequest, user: User = Depends(get_current_admin)):
    try:
        res = await asyncio.to_thread(ontology_service.apply, req.define)
        rec = await asyncio.to_thread(
            ontology_versions.record, _who(user), "apply", req.define, req.comment,
            bool(res.get("ok")), "" if res.get("ok") else res.get("error", ""),
        )
        if res.get("ok"):
            res["version_id"] = rec.get("id", "")
        return res
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/type", summary="Создать тип (entity/relation/attribute)")
async def ontology_create_type(req: TypeRequest, user: User = Depends(get_current_admin)):
    try:
        res = await asyncio.to_thread(
            ontology_service.create_type, req.kind, req.label, req.owns, req.roles, req.value
        )
        text = f"создание {req.kind} {req.label}"
        rec = await asyncio.to_thread(
            ontology_versions.record, _who(user), "create", text, req.comment,
            bool(res.get("ok")), "" if res.get("ok") else res.get("error", ""),
        )
        if res.get("ok"):
            res["version_id"] = rec.get("id", "")
        return res
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.delete("/type/{kind}/{label}", summary="Удалить тип")
async def ontology_delete_type(kind: str, label: str, user: User = Depends(get_current_admin)):
    try:
        res = await asyncio.to_thread(ontology_service.delete_type, kind, label)
        text = f"удаление {kind} {label}"
        rec = await asyncio.to_thread(
            ontology_versions.record, _who(user), "delete", text, "",
            bool(res.get("ok")), "" if res.get("ok") else res.get("error", ""),
        )
        if res.get("ok"):
            res["version_id"] = rec.get("id", "")
        return res
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ---- журнал версий ----------------------------------------------------
@router.get("/versions", summary="История изменений схемы")
async def ontology_versions_list(limit: int = 50, _: User = Depends(get_current_admin)):
    return {"versions": await asyncio.to_thread(ontology_versions.list_versions, limit)}


@router.get("/versions/{version_id}", summary="Одна версия со снапшотом и текстом")
async def ontology_version_get(version_id: str, _: User = Depends(get_current_admin)):
    row = await asyncio.to_thread(ontology_versions.get_version, version_id)
    if not row:
        raise HTTPException(status_code=404, detail="версия не найдена")
    return row


@router.get("/diff", summary="Что изменится при откате к версии (без применения)")
async def ontology_diff(version_id: str, _: User = Depends(get_current_admin)):
    import json as _json

    target = await asyncio.to_thread(ontology_versions.get_version, version_id)
    if not target:
        raise HTTPException(status_code=404, detail="версия не найдена")
    try:
        current = await asyncio.to_thread(lambda: snapshot_of(ontology_service.get_schema()))
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        target_snap = _json.loads(target.get("snapshot_json") or "{}")
    except Exception:
        raise HTTPException(status_code=500, detail="снапшот версии повреждён")
    return diff_snapshots(current, target_snap)


@router.post("/versions/{version_id}/restore", summary="Откатить схему к версии")
async def ontology_restore(version_id: str, user: User = Depends(get_current_admin)):
    try:
        return await asyncio.to_thread(ontology_versions.restore, version_id, _who(user), "")
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/undo", summary="Отменить последнее изменение схемы")
async def ontology_undo(req: PositionRequest, user: User = Depends(get_current_admin)):
    try:
        prev = await asyncio.to_thread(ontology_versions.previous_version, req.current_version_id)
        if not prev:
            return {"ok": False, "error": "предыдущей версии нет — это первое изменение в журнале"}
        res = await asyncio.to_thread(ontology_versions.restore, prev["id"], _who(user), "отмена изменения")
        res["position"] = prev["id"]
        return res
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/redo", summary="Повторить отменённое изменение")
async def ontology_redo(req: PositionRequest, user: User = Depends(get_current_admin)):
    if not req.current_version_id:
        return {"ok": False, "error": "неизвестна текущая позиция в журнале"}
    try:
        nxt = await asyncio.to_thread(ontology_versions.next_version, req.current_version_id)
        if not nxt:
            return {"ok": False, "error": "повторять нечего — новее версий нет"}
        res = await asyncio.to_thread(ontology_versions.restore, nxt["id"], _who(user), "повтор изменения")
        res["position"] = nxt["id"]
        return res
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ---- шаблон и экспорт -------------------------------------------------
@router.get("/export", summary="Скачать текущую схему как .tql")
async def ontology_export(_: User = Depends(get_current_admin)):
    try:
        schema = await asyncio.to_thread(ontology_service.get_schema)
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    text = ontology_service.to_tql(
        snapshot_of(schema),
        comment=(
            "Схема онтологии KAG (TypeDB), сгенерирована из живого состояния.\n"
            "Формат TypeQL 3.x: каждый owns/relates/plays — отдельным определением,\n"
            "несколько инструкций define в одном тексте недопустимы."
        ),
    )
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ontology.tql"'},
    )


@router.post("/template", summary="Проверить и применить шаблон .tql")
async def ontology_template(req: TemplateRequest, user: User = Depends(get_current_admin)):
    """Импорт шаблона: сначала проверка (ничего не меняет), затем применение тем же текстом."""
    text = (req.text or "").strip()
    if not text:
        return {"ok": False, "error": "пустой файл"}

    def _run() -> dict:
        check = ontology_service.validate(text)
        if not check.get("ok"):
            ontology_versions.record(_who(user), "template", text,
                                     req.comment or "шаблон отклонён проверкой", False,
                                     check.get("error", ""))
            return {"ok": False, "stage": "validate", "error": check.get("error", "")}
        res = ontology_service.apply(text)
        rec = ontology_versions.record(
            _who(user), "template", text,
            req.comment or ("шаблон применён" if res.get("ok") else "шаблон не применён"),
            bool(res.get("ok")), "" if res.get("ok") else res.get("error", ""),
        )
        res["stage"] = "applied" if res.get("ok") else "apply"
        if res.get("ok"):
            res["version_id"] = rec.get("id", "")
        return res

    try:
        return await asyncio.to_thread(_run)
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
