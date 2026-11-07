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
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from src.api.middleware.auth_v2 import get_current_admin
from src.api.services.ontology_service import OntologyUnavailable, ontology_service
from src.api.services.ontology_versions import diff_snapshots, ontology_versions, snapshot_of
from src.api.services.ontology_prompts import DEFAULTS, prompt_store
from src.api.services.proposal_service import ALLOWED_STATUSES, proposal_service
from src.database.user_models import User
from src.indexing import typedb_facts

logger = logging.getLogger(__name__)

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
    target_version_id: str = Field(default="", description="Куда повторять (для redo)")


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
        # Точка, от которой отменяем: позиция пользователя, а если её нет — последняя версия.
        cur = req.current_version_id
        if not cur:
            latest = await asyncio.to_thread(ontology_versions.latest_version)
            cur = (latest or {}).get("id", "")
        prev = await asyncio.to_thread(ontology_versions.previous_version, cur)
        if not prev:
            return {"ok": False, "error": "предыдущей версии нет — это первое изменение в журнале"}
        res = await asyncio.to_thread(ontology_versions.restore, prev["id"], _who(user), "отмена изменения")
        res["position"] = prev["id"]
        # Куда вернуться по «Повторить»: состояние, от которого отменили.
        res["redo_target"] = cur
        return res
    except OntologyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/redo", summary="Повторить отменённое изменение")
async def ontology_redo(req: PositionRequest, user: User = Depends(get_current_admin)):
    # Повтор — это возврат к состоянию, от которого отменяли, а НЕ «следующая версия в журнале»:
    # сам откат тоже пишет версию, и поиск «следующей» возвращал бы запись отката (петля).
    target = req.target_version_id
    if not target:
        return {"ok": False, "error": "неизвестно, что повторять: сначала отмените изменение"}
    try:
        res = await asyncio.to_thread(ontology_versions.restore, target, _who(user), "повтор изменения")
        res["position"] = target
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


# ═══════════════════════════════════════════════════════════════════════════
# ОЧЕРЕДЬ ПРЕДЛОЖЕНИЙ (human-in-the-loop)
#
# Извлечённые из документов требования попадают сюда, а НЕ сразу в TypeDB.
# Причина: логический вывод строит «нарушения», и если правила увидят
# непроверенный факт, заказчик получит ложное нарушение с цитатой из мусора.
# Пока status='proposed', в онтологии этих фактов нет физически.
# ═══════════════════════════════════════════════════════════════════════════

class ReviewRequest(BaseModel):
    status: str = Field(..., description="proposed | accepted | rejected | needs_schema")
    note: str | None = Field(None, description="комментарий проверяющего")
    text: str | None = Field(None, description="исправленная формулировка требования")


class BulkReviewRequest(BaseModel):
    ids: List[str]
    status: str
    note: str | None = None


def _check_status(status: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"недопустимый статус '{status}', разрешены: {', '.join(ALLOWED_STATUSES)}",
        )


@router.get("/proposals/stats", summary="Сводка очереди предложений")
async def proposals_stats(_: User = Depends(get_current_admin)):
    """Счётчики по статусам, документам и областям — для плиток в шапке страницы."""
    return await asyncio.to_thread(proposal_service.stats)


@router.get("/proposals/documents", summary="Документы, у которых есть предложения")
async def proposals_documents(_: User = Depends(get_current_admin)):
    return {"items": await asyncio.to_thread(proposal_service.documents)}


@router.get("/proposals", summary="Список предложений с фильтрами")
async def proposals_list(
    status: str = "proposed",
    document_id: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _: User = Depends(get_current_admin),
):
    """status='all' — без фильтра по статусу."""
    return await asyncio.to_thread(
        proposal_service.list_proposals,
        status=status,
        document_id=document_id,
        kind=kind,
        query=q,
        scope=scope,
        limit=limit,
        offset=offset,
    )


@router.post("/proposals/review-bulk", summary="Принять/отклонить пачкой")
async def proposals_bulk_review(req: BulkReviewRequest, user: User = Depends(get_current_admin)):
    _check_status(req.status)
    changed = await asyncio.to_thread(
        proposal_service.bulk_review, req.ids, req.status, _who(user), req.note
    )
    return {"ok": True, "changed": changed}


@router.get("/proposals/{proposal_id}", summary="Одно предложение целиком")
async def proposals_get(proposal_id: str, _: User = Depends(get_current_admin)):
    item = await asyncio.to_thread(proposal_service.get, proposal_id)
    if not item:
        raise HTTPException(status_code=404, detail="предложение не найдено")
    return item


@router.post("/proposals/{proposal_id}/review", summary="Решение по предложению")
async def proposals_review(
    proposal_id: str, req: ReviewRequest, user: User = Depends(get_current_admin)
):
    """Проверяющий принимает, отклоняет или правит формулировку.

    Принятие пишет факт в TypeDB сразу: очередь и онтология — разные хранилища,
    и без этого шага «принято» означало только смену статуса в Postgres
    (у всех accepted typedb_fact_id оставался пустым). Ошибка записи не отменяет
    решение человека — она сохраняется в write_error, чтобы её было видно.
    """
    _check_status(req.status)
    if req.text is not None and not req.text.strip():
        raise HTTPException(status_code=400, detail="пустая формулировка недопустима")
    res = await asyncio.to_thread(
        proposal_service.review,
        proposal_id, req.status, _who(user), req.note, req.text,
    )
    if not res:
        raise HTTPException(status_code=404, detail="предложение не найдено")

    write_info: dict = {"status": "not_applicable"}
    if req.status == "accepted":
        try:
            from src.indexing import typedb_writer

            if typedb_writer.is_available() and typedb_writer.write_enabled():
                item = await asyncio.to_thread(proposal_service.get, proposal_id)
                if item:
                    done = await asyncio.to_thread(typedb_writer.write_proposal, item)
                    await asyncio.to_thread(
                        proposal_service.set_write_result, proposal_id, done["fact_id"], None
                    )
                    write_info = {"status": "written", "fact_id": done["fact_id"],
                                  "written": done.get("written")}
                else:
                    write_info = {"status": "skipped", "reason": "предложение не прочитано"}
            else:
                write_info = {"status": "skipped", "reason": "typedb выключен"}
        except Exception as exc:  # noqa: BLE001 — решение человека важнее записи
            msg = str(exc)[:300]
            logger.warning("запись принятого предложения %s в TypeDB не удалась: %s",
                           proposal_id, msg)
            try:
                await asyncio.to_thread(
                    proposal_service.set_write_result, proposal_id, None, msg
                )
            except Exception:  # noqa: BLE001
                pass
            write_info = {"status": "error", "error": msg}

    return {"ok": True, "item": res, "typedb": write_info}


# ── Промпт извлечения сущностей ───────────────────────────────────────────
# Текст, по которому модель превращает фрагмент нормативного документа в
# сущности и N-арные связи TypeDB. Хранится в БД с историей версий, а не в
# коде: правка не требует пересборки образа, но каждая версия видна и
# откатываема. Рядом с «Онтологией», потому что прямо на неё влияет.

class PromptBody(BaseModel):
    content: str = Field(..., description="полный текст промпта с плейсхолдером {text_chunk}")


@router.get("/prompt/{name}", summary="Текущий промпт извлечения и последствия правки")
async def prompt_get(name: str, _: User = Depends(get_current_admin)):
    try:
        return await asyncio.to_thread(prompt_store.get, name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"промпт {name} не существует")
    except Exception as exc:  # БД недоступна и т.п.
        raise HTTPException(status_code=503, detail=f"хранилище промптов недоступно: {exc}")


@router.put("/prompt/{name}", summary="Сохранить новую версию промпта")
async def prompt_save(name: str, req: PromptBody, user: User = Depends(get_current_admin)):
    if name not in DEFAULTS:
        raise HTTPException(status_code=404, detail=f"промпт {name} не существует")
    res = await asyncio.to_thread(prompt_store.save, name, req.content, _who(user))
    if res.get("status") == "error":
        return {"ok": False, "problems": res.get("problems", [])}
    res["ok"] = True
    return res


@router.post("/prompt/{name}/reset", summary="Вернуть заводской текст промпта")
async def prompt_reset(name: str, user: User = Depends(get_current_admin)):
    if name not in DEFAULTS:
        raise HTTPException(status_code=404, detail=f"промпт {name} не существует")
    res = await asyncio.to_thread(prompt_store.reset, name, _who(user))
    res["ok"] = True
    return res


@router.get("/prompt/{name}/history", summary="История версий промпта")
async def prompt_history(name: str, limit: int = 20, _: User = Depends(get_current_admin)):
    return {"ok": True, "items": await asyncio.to_thread(prompt_store.history, name, limit)}


@router.get("/prompt/{name}/history/{version}", summary="Текст конкретной версии промпта")
async def prompt_history_version(name: str, version: int, _: User = Depends(get_current_admin)):
    content = await asyncio.to_thread(prompt_store.history_version, name, version)
    if content is None:
        raise HTTPException(status_code=404, detail="версия не найдена")
    return {"ok": True, "version": version, "content": content}
