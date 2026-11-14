"""Маршруты страницы «Конвейер обработки»: этапы, время, включение сбора."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
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


class ParallelBody(BaseModel):
    parallel: int = Field(..., ge=1, le=16,
                          description="сколько фрагментов одновременно отправлять в модель")


@router.get("/parallel", summary="Параллельность извлечения (сколько фрагментов сразу в модель)")
async def pipeline_parallel(_: User = Depends(get_current_admin)):
    from src.api.services import pipeline

    return {"ok": True, "parallel": await asyncio.to_thread(pipeline.extraction_parallel),
            "min": pipeline.PARALLEL_MIN, "max": pipeline.PARALLEL_MAX}


@router.post("/parallel", summary="Задать параллельность извлечения")
async def pipeline_parallel_set(body: ParallelBody, user: User = Depends(get_current_admin)):
    from src.api.services import pipeline

    n = await asyncio.to_thread(pipeline.set_extraction_parallel, body.parallel,
                                getattr(user, "email", "") or "")
    return {"ok": True, "parallel": n,
            "message": f"Параллельность извлечения: {n} фрагментов одновременно"}


class QueueResolveBody(BaseModel):
    action: str = Field(..., description="accept | reject")
    note: str = Field("", description="комментарий человека (необязательно)")


# ВАЖНО: маршруты /bronze/queue* объявлены ДО /bronze/{document_id}, иначе «queue» попадёт
# в параметр document_id и вернётся пустой прогон вместо очереди.
@router.get("/bronze/queue/runs", summary="Прогоны документа для выбора очереди прошлого прогона")
async def pipeline_queue_runs(document_id: str, limit: int = 20,
                              _: User = Depends(get_current_admin)):
    from src.indexing import bronze

    items = await asyncio.to_thread(bronze.runs, document_id, limit)
    return {"ok": True, "document_id": document_id,
            "items": [{"run_id": r["run_id"], "started_at": r.get("started_at"),
                       "chunks": r.get("chunks"), "empty_chunks": r.get("empty_chunks"),
                       "prompt_version": r.get("prompt_version"),
                       "model": r.get("model")} for r in items]}


@router.get("/bronze/queue", summary="Очередь разбора Silver-слоя (спорные записи)")
async def pipeline_queue(run_id: str = "", reason: str = "", status: str = "pending",
                         limit: int = 100, offset: int = 0,
                         _: User = Depends(get_current_admin)):
    from src.indexing import bronze

    page = await asyncio.to_thread(bronze.queue_list, run_id, reason, status, limit, offset)
    stats = await asyncio.to_thread(bronze.queue_stats, run_id)
    return {"ok": True, **page, "stats": stats}


@router.post("/bronze/queue/{queue_id}/resolve", summary="Решение по записи очереди")
async def pipeline_queue_resolve(queue_id: int, body: QueueResolveBody,
                                 user: User = Depends(get_current_admin)):
    from src.indexing import bronze

    if body.action not in ("accept", "reject"):
        raise HTTPException(status_code=400, detail="action must be accept|reject")
    ok = await asyncio.to_thread(bronze.queue_resolve, queue_id, body.action,
                                 getattr(user, "email", "") or "", body.note)
    return {"ok": ok, "status": "resolved" if ok else "already_resolved",
            "message": "решение записано" if ok else "запись уже разрешена"}


@router.get("/bronze/{document_id}", summary="Прогоны извлечения документа (Bronze-слой)")
async def pipeline_bronze_runs(document_id: str, limit: int = 20,
                               _: User = Depends(get_current_admin)):
    """Список прогонов извлечения: сколько сущностей/связей дал каждый прогон.

    Отвечает на вопрос «почему в этот раз меньше, чем в прошлый»: сравнение прогонов
    по ключам, а не по общему числу.
    """
    from src.indexing import bronze

    return {"ok": True, "document_id": document_id,
            "items": await asyncio.to_thread(bronze.runs, document_id, limit)}


@router.get("/bronze/{document_id}/compare", summary="Диф двух прогонов извлечения")
async def pipeline_bronze_compare(document_id: str, run_a: str, run_b: str,
                                  _: User = Depends(get_current_admin)):
    from src.indexing import bronze

    return {"ok": True, **(await asyncio.to_thread(bronze.compare, document_id, run_a, run_b))}


@router.get("/bronze/{document_id}/clusters",
            summary="Нормализация и блокировка: сколько разных объектов в сырье прогона")
async def pipeline_bronze_clusters(document_id: str, run_id: str = "",
                                   _: User = Depends(get_current_admin)):
    """Silver-шаг: нормализация строк и блокировка по ключам, без слияния.

    Отвечает на вопрос «2204 строки в Bronze — это сколько разных объектов?»
    Для каждого типа: сколько строк, сколько кластеров, крупнейшие кластеры.
    """
    from src.indexing import bronze, bronze_resolve

    if not run_id:
        runs = await asyncio.to_thread(bronze.runs, document_id, 1)
        if not runs:
            return {"ok": False, "error": "прогонов для документа нет"}
        run_id = runs[0]["run_id"]
    return {"ok": True, **(await asyncio.to_thread(bronze_resolve.clusters, run_id))}


@router.get("/bronze/{document_id}/prompt-stats",
            summary="Эффективность по версиям промпта извлечения")
async def pipeline_bronze_prompt_stats(document_id: str, limit: int = 20,
                                       _: User = Depends(get_current_admin)):
    """Сколько сырья дала каждая версия промпта: основа для сравнения версий."""
    from src.indexing import bronze, bronze_resolve

    runs = await asyncio.to_thread(bronze.runs, document_id, limit)
    stats = await asyncio.to_thread(bronze_resolve.by_prompt, [r["run_id"] for r in runs])
    return {"ok": True, "document_id": document_id, "runs": len(runs), **stats}


@router.post("/collect", summary="Включить или выключить сбор статистики этапов")
async def pipeline_collect(body: CollectBody, user: User = Depends(get_current_admin)):
    from src.api.services import pipeline

    ok = await asyncio.to_thread(pipeline.set_enabled, body.enabled,
                                 getattr(user, "email", "") or "")
    return {"ok": bool(ok), "collecting": body.enabled,
            "message": "Сбор статистики включён" if body.enabled else "Сбор статистики выключен"}
