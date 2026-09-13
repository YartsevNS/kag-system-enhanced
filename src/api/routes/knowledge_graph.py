"""
API-роуты для Knowledge Graph (Neo4j).
"""

import asyncio

from fastapi import APIRouter, HTTPException, Depends, Body
from typing import Literal, Optional, List
from loguru import logger

from src.api.middleware.auth_v2 import get_current_user_optional, get_current_admin
from src.indexing.knowledge_graph import kg_service
from src.indexing.ids import display_filename
from src.database.user_models import User
from neo4j.exceptions import ClientError as Neo4jClientError

def _clamp(value, low: int = 1, high: int = 1000) -> int:
    """Ограничить лимит сверху: limit=100000 не должен тянуть весь граф."""
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return low


router = APIRouter()


@router.post("/cypher", summary="Произвольный Cypher-запрос")
async def execute_cypher(
    query: dict = Body(...),
    current_user: User = Depends(get_current_admin)
):
    """Выполнение произвольного Cypher-запроса (только чтение)."""
    try:
        q = query.get("query", "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="Пустой запрос")
        limit = int(query.get("limit", 100))
        results = await asyncio.to_thread(kg_service.execute_cypher, q, _clamp(limit))
        # Аудит: единственная точка произвольного Cypher. Пишем кто, что и сколько
        # вернулось (без самих данных — там могут быть тексты документов).
        logger.info(
            f"[cypher] user={getattr(current_user, 'username', '?')} "
            f"limit={_clamp(limit)} rows={len(results)} query={q[:200]!r}"
        )
        return {"query": q, "results": results, "total": len(results)}
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Neo4jClientError as e:
        raise HTTPException(status_code=400, detail=f"Ошибка запроса: {e}")
    except Exception as e:
        logger.error(f"Ошибка Cypher: {e}")
        raise HTTPException(status_code=502, detail=f"Ошибка выполнения в Neo4j: {e}")


@router.get("/stats", summary="Статистика графа знаний")
async def kg_stats():
    """Статистика: количество документов, чанков, сущностей, связей.

    Параметр current_user не нужен: доступ к /api/v1/kg требует аутентификации
    на уровне middleware, а значение тут не используется.
    """
    try:
        return await asyncio.to_thread(kg_service.get_stats)
    except Exception as e:
        logger.error(f"Ошибка статистики графа: {e}")
        # Нули вводили в заблуждение: админ видел «граф пуст» и мог запустить
        # перестроение. Отдаём явную ошибку.
        raise HTTPException(status_code=503, detail=f"Статистика графа недоступна: {e}")


@router.get("/entities/search", summary="Поиск сущностей")
async def search_entities(
    q: str, 
    entity_type: Optional[str] = None, 
    limit: int = 20,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Поиск сущностей по имени и типу."""
    try:
        found = await asyncio.to_thread(kg_service.search_entities, q, entity_type, _clamp(limit, 1, 200))
        return {"results": found}
    except Exception as e:
        return {"results": [], "error": str(e)}


@router.get("/entities/{document_id}", summary="Сущности документа")
async def document_entities(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Все сущности, извлечённые из документа."""
    try:
        entities = await asyncio.to_thread(kg_service.get_document_entities, document_id)
        return {"document_id": document_id, "entities": entities, "total": len(entities)}
    except Exception as e:
        return {"document_id": document_id, "entities": [], "total": 0, "error": str(e)}


@router.get("/graph/{entity_name:path}", summary="Подграф сущности")
async def entity_graph(
    entity_name: str, 
    depth: int = 2,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Подграф вокруг сущности (узлы + связи)."""
    try:
        graph = await asyncio.to_thread(kg_service.get_entity_graph, entity_name, depth)
        return {"entity": entity_name, "graph": graph}
    except Exception as e:
        return {"entity": entity_name, "graph": [], "error": str(e)}


def _page_of(payload: dict):
    """Номер страницы чанка из payload Qdrant.

    Форматы в базе разные: верхний уровень "page", metadata.page_number и
    metadata.pages (СПИСОК — так пишет текущий код индексации). Для диапазона
    возвращаем "первая-последняя", чтобы просмотрщик открылся на нужной странице.
    """
    if not payload:
        return None
    meta = payload.get("metadata") or {}
    one = payload.get("page") or meta.get("page_number") or meta.get("page")
    if one:
        return one
    pages = meta.get("pages") or payload.get("pages")
    if isinstance(pages, (list, tuple)) and pages:
        try:
            first, last = pages[0], pages[-1]
            return first if str(first) == str(last) else f"{first}-{last}"
        except Exception:
            return pages[0]
    return None


@router.get("/entity/{entity_name:path}/chunks",
            summary="Чанки, где упоминается сущность (текст и страница из Qdrant)")
async def entity_chunks(
    entity_name: str,
    limit: int = 8,
    doc_id: str = "",
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Чанки сущности: номер фрагмента, документ, превью и полный текст.

    Превью берём из графа (там полная копия текста), а номер СТРАНИЦЫ и полный
    текст — из payload Qdrant: в графе страницы нет. Именно этого не хватало в
    /kg, чтобы от сущности перейти к фрагменту и к файлу.
    """
    try:
        from src.indexing.embeddings_service import embeddings_service

        rows = await asyncio.to_thread(
            kg_service.entity_chunks, entity_name, _clamp(limit, 1, 50), doc_id or ""
        )
        point_ids = [r.get("point_id") for r in rows if r.get("point_id")]
        payloads = await embeddings_service.get_points_payload(point_ids) if point_ids else {}
        for r in rows:
            pl = payloads.get(str(r.get("point_id") or ""), {}) or {}
            r["page"] = _page_of(pl)
            if pl.get("content"):
                r["content"] = pl["content"]
            if pl.get("filename"):
                r["filename"] = display_filename(pl["filename"])
            if pl.get("document_id"):
                r["document_id"] = pl["document_id"]
        return {"entity": entity_name, "chunks": rows, "total": len(rows)}
    except Exception as e:
        logger.warning(f"Ошибка чанков сущности «{entity_name}»: {e}")
        return {"entity": entity_name, "chunks": [], "error": str(e)}


@router.get("/search-chunks",
            summary="Найти фрагменты по тексту + соседние по уровню (фильтр по документу)")
async def search_chunks(
    q: str,
    doc_id: str = "",
    level: int = 1,
    limit: int = 20,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Поиск фрагментов в графе: совпадения плюс соседи по chunk_seq (±level).

    Затем в браузер уходит только эта выборка — не весь документ.
    """
    try:
        result = await asyncio.to_thread(
            kg_service.search_chunks, q, doc_id or "", level, _clamp(limit, 1, 60)
        )
        if not result:
            return {"query": q, "nodes": [], "edges": [], "hits": 0, "chunks_shown": 0}
        result["query"] = q
        return result
    except Exception as e:
        logger.warning(f"Ошибка поиска фрагментов «{q}»: {e}")
        return {"query": q, "nodes": [], "edges": [], "hits": 0, "chunks_shown": 0,
                "error": str(e)}


@router.get("/document/{document_id}/graph",
            summary="Подграф документа: фрагменты и сущности (фильтр по документу)")
async def document_graph(
    document_id: str,
    with_chunks: bool = True,
    limit_chunks: int = 150,
    limit_entities: int = 120,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Граф одного документа: какие сущности пришли из этого файла.

    with_chunks=false — без узлов-фрагментов (для очень крупных документов).
    """
    try:
        graph = await asyncio.to_thread(
            kg_service.get_document_graph, document_id, with_chunks,
                                              _clamp(limit_chunks, 1, 500),
                                              _clamp(limit_entities, 1, 500))
        if not graph:
            return {"document_id": document_id, "graph": [],
                    "error": "документ не найден в графе знаний"}
        return {"document_id": document_id, "graph": graph}
    except Exception as e:
        logger.warning(f"Ошибка подграфа документа {document_id}: {e}")
        return {"document_id": document_id, "graph": [], "error": str(e)}


@router.get("/chunk/{chunk_id}", summary="Чанк: текст, документ, страница")
async def chunk_details(
    chunk_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Один чанк по id — текст (из Qdrant, если есть) и страница для перехода."""
    try:
        from src.indexing.embeddings_service import embeddings_service

        info = await asyncio.to_thread(kg_service.chunk_info, chunk_id)
        if not info:
            return {"chunk_id": chunk_id, "found": False}
        point_id = info.get("point_id")
        payloads = await embeddings_service.get_points_payload([point_id]) if point_id else {}
        pl = payloads.get(str(point_id), {}) or {}
        info["page"] = _page_of(pl)
        if pl.get("content"):
            info["content"] = pl["content"]
        if pl.get("filename"):
            info["filename"] = display_filename(pl["filename"])
        if pl.get("document_id"):
            info["document_id"] = pl["document_id"]
        info["found"] = True
        return info
    except Exception as e:
        logger.warning(f"Ошибка чанка «{chunk_id}»: {e}")
        return {"chunk_id": chunk_id, "found": False, "error": str(e)}


@router.get("/hybrid-search", summary="Гибридный поиск")
async def hybrid_search(
    q: str, 
    doc_id: Optional[str] = None,
    scope: Literal["both", "neo4j", "qdrant"] = "both",
    relevance_score: Optional[float] = 0.4,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Гибридный поиск: граф (Neo4j) + вектор (Qdrant).
    
    scope: "both" | "neo4j" | "qdrant"
    relevance_score: минимальный score для Qdrant-результатов (0 = без фильтра)
    """
    try:
        from src.indexing.embeddings_service import embeddings_service
        
        entities = [e.strip() for e in q.split(",") if e.strip()]
        doc_ids = [doc_id] if doc_id else None
        results = []
        
        # 1. Поиск в графе Neo4j
        if scope != "qdrant":
            results = await asyncio.to_thread(kg_service.hybrid_search, entities, doc_ids) if entities else []
        
        # 2. Если граф ничего не нашёл или scope=qdrant — ищем через Qdrant
        if not results or scope == "qdrant":
            try:
                # initialize() создаёт клиент и проверяет коллекцию — на каждый запрос это
                # лишний сетевой круг. Инициализируем, только если клиента ещё нет.
                if not embeddings_service.is_initialized():
                    await embeddings_service.initialize()
                qdrant_results = await embeddings_service.search(q, limit=20)
                seen_texts = set()
                q_lower = q.lower()  # не зависит от итерации — считаем один раз
                for point in (qdrant_results or []):
                    score = point.get("score", 0)
                    content = (point.get("content", "") or "").strip()
                    # Фильтр по релевантности
                    threshold = float(relevance_score or 0)
                    if threshold > 0 and score < threshold:
                        continue
                    if not content:
                        continue
                    text_key = content[:100]
                    if text_key in seen_texts:
                        continue
                    # Буст: если query встречается в тексте
                    if q_lower in content.lower():
                        score += 0.3
                    seen_texts.add(text_key)
                    results.append({
                        "chunk_id": point.get("chunk_id", ""),
                        # Усечение осознанное: в Qdrant текст полный, обозревателю /kg
                        # достаточно превью — тот же лимит, что для графовых
                        # результатов (knowledge_graph.CHUNK_TEXT_PREVIEW_CHARS).
                        "text": content[:CHUNK_TEXT_PREVIEW_CHARS],
                        "doc_id": point.get("document_id", ""),
                        # ВАЖНО: filename из payload-поля filename, НЕ file_type!
                        # Была ошибка: point.get("file_type") — подставлялся MIME-тип
                        # (application/pdf, .txt) вместо имени файла → кракозябры.
                        "filename": point.get("filename") or point.get("file_name") or "",
                        # Страницы, где взят чанк (из metadata сегментного чанкинга)
                        "pages": (point.get("metadata") or {}).get("pages", [])
                            if isinstance(point.get("metadata"), dict) else [],
                        "score": round(score, 4),
                        "entity_count": 0,
                        "source": "qdrant"
                    })
                # Сортируем по score
                results.sort(key=lambda r: r.get("score", 0), reverse=True)
            except Exception as e:
                logger.warning(f"Qdrant fallback failed: {e}")
        
        return {"query": q, "results": results, "total": len(results)}
    except Exception as e:
        return {"query": q, "results": [], "total": 0, "error": str(e)}


@router.post("/rebuild-graph", summary="Перестроить граф для существующих документов (в фоне)")
async def rebuild_graph(
    document_ids: Optional[List[str]] = Body(None, embed=True),
    current_user: User = Depends(get_current_admin)
):
    """Запустить фоновое перестроение графа знаний.

    Тяжёлая работа (LLM-извлечение сущностей по всем документам) выполняется
    в Celery-задаче rebuild_graph_task, эндпоинт возвращает сразу.
    Прогресс можно смотреть через GET /rebuild-status.
    Если document_ids=None — обработать все документы со статусом completed.
    """
    from src.api.services.config_store import config_store

    # Гонка check-then-act: два параллельных запроса оба видели «не running» и
    # запускали задачу дважды. Флаг захватываем атомарно (compare-and-set).
    status = config_store.get("kg_config", "rebuild_status") or "idle"
    # Сначала явное «уже идёт» (значение уже running), и только потом «кто-то
    # опередил» (CAS не прошёл из-за конкурентного запроса). Иначе при status
    # == "running" CAS(running→running) проходит, и смысл двух сообщений
    # смазывается.
    if status == "running":
        raise HTTPException(status_code=409, detail="Перестроение графа уже идёт")
    if not config_store.compare_and_set("kg_config", "rebuild_status", "running", status):
        raise HTTPException(status_code=409, detail="Перестроение графа уже запущено")
    logger.info(
        f"[rebuild] запуск: user={getattr(current_user, 'username', '?')} "
        f"документов={len(document_ids) if document_ids else 'все completed'}"
    )

    try:
        from src.indexing.tasks import rebuild_graph_task
        config_store.set("kg_config", "rebuild_stop", False)
        config_store.set("kg_config", "rebuild_status", "running")
        config_store.set("kg_config", "rebuild_progress", {
            "processed": 0, "total": 0, "current_doc": "",
            "started_at": "", "finished_at": "",
        })
        rebuild_graph_task.delay(document_ids=document_ids)
        logger.info(f"Перестроение графа поставлено в очередь (документов: {len(document_ids) if document_ids else 'все completed'})")
        return {"status": "ok", "started": True,
                "message": "Перестроение запущено в фоне"}
    except Exception as e:
        config_store.set("kg_config", "rebuild_status", "error")
        logger.error(f"Ошибка запуска перестроения графа: {e}")
        raise HTTPException(status_code=503, detail=f"Не удалось запустить перестроение: {e}")


@router.get("/rebuild-status", summary="Статус перестроения графа")
async def rebuild_status(current_user: Optional[User] = Depends(get_current_user_optional)):
    """Статус фонового перестроения графа (для страницы /kg)."""
    try:
        from src.api.services.config_store import config_store
        status = config_store.get("kg_config", "rebuild_status") or "idle"
        progress = config_store.get("kg_config", "rebuild_progress") or {}
        return {
            "status": status,
            "processed": progress.get("processed", 0),
            "total": progress.get("total", 0),
            "current_doc": progress.get("current_doc", ""),
            "started_at": progress.get("started_at", ""),
            "finished_at": progress.get("finished_at", ""),
        }
    except Exception as e:
        # Отличаем сбой чтения статуса от падения самой задачи перестроения
        return {"status": "error", "transport_error": True,
                "message": f"не удалось прочитать статус: {e}"}


# ============================================================
# Пост-обработка и валидация (Neo4j Best Practices)
# ============================================================

@router.post("/post-process", summary="Пост-обработка графа")
async def post_process_graph(
    document_id: Optional[str] = None,
    current_user: User = Depends(get_current_admin)
):
    """
    Запустить пост-обработку графа: dedup, entity linking.
    
    Опционально: только для одного документа.
    """
    try:
        result = await asyncio.to_thread(kg_service.post_process_entities, document_id)
        # Также простой dedup для Community Edition
        dedup_count = await asyncio.to_thread(kg_service.deduplicate_entities_by_name)
        result["dedup_count"] = dedup_count
        return {"status": "ok", **result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/stop-rebuild", summary="Остановить перестроение графа")
async def stop_rebuild(current_user: User = Depends(get_current_admin)):
    """Установить флаг остановки перестроения графа знаний."""
    try:
        from src.api.services.config_store import config_store
        config_store.set("kg_config", "rebuild_stop", True)
        return {"status": "ok", "message": "Сигнал остановки отправлен. Текущий документ будет последним."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/validate/{document_id}", summary="Валидация сущностей документа")
async def validate_document_entities(document_id: str, current_user: User = Depends(get_current_admin)):
    """Проверить качество извлечённых сущностей (admin-only)."""
    try:
        result = await asyncio.to_thread(kg_service.validate_entities, document_id)
        return result
    except Exception as e:
        return {"valid": False, "error": str(e)}


@router.get("/domain-schema", summary="Доменная схема сущностей")
async def get_domain_schema(current_user: Optional[User] = Depends(get_current_user_optional)):
    """Получить текущую доменную схему + список доступных пресетов."""
    try:
        from src.indexing.entity_extractor import entity_extractor
        from src.indexing.entity_extractor import EntityExtractor
        # Отдаём состояние ИЗ НАСТРОЕК: пресет живёт в config_store, память
        # процесса обнуляется при рестарте, и интерфейс иначе показывал бы
        # «universal» после каждого перезапуска api.
        await asyncio.to_thread(entity_extractor.apply_stored_domain_schema)
        return {
            "schema": entity_extractor._domain_config,
            "active_preset": EntityExtractor.get_active_preset(),
            "presets": EntityExtractor.get_presets()
        }
    except Exception as e:
        return {"error": str(e)}


@router.post("/domain-schema", summary="Обновить доменную схему")
async def update_domain_schema(
    data: dict,
    current_user: User = Depends(get_current_admin)
):
    """
    Обновить доменную схему сущностей.
    
    Два режима:
    - Переключение пресета: {"preset": "accounting"}
    - Ручная схема: {"core": {...}, "relations": {...}, "extended": {...}}
    """
    try:
        from src.indexing.entity_extractor import entity_extractor, EntityExtractor
        from src.api.services.config_store import config_store
        
        # Режим 1: переключение пресета
        if "preset" in data:
            preset_name = data["preset"]
            result = EntityExtractor.switch_preset(preset_name)
            if "error" in result:
                return {"status": "error", "message": result["error"]}
            # Обновляем активную схему в экстракторе
            entity_extractor.set_domain_schema(
                dict(EntityExtractor.SCHEMA_PRESETS[preset_name]["schema"])
            )
            await asyncio.to_thread(
                kg_service.set_domain_schema,
                EntityExtractor.SCHEMA_PRESETS[preset_name]["schema"].get("core", {}),
            )
            # Сохраняем ДЕЙСТВУЮЩУЮ схему: память процесса переживает только
            # сам запрос, worker — другой процесс и без этой записи пресет не увидит.
            config_store.set("kg_config", EntityExtractor.DOMAIN_ACTIVE_KEY, {
                "mode": "preset",
                "preset": preset_name,
                "schema": EntityExtractor.SCHEMA_PRESETS[preset_name]["schema"],
            })
            logger.info(f"[domain] пресет «{preset_name}» сохранён в настройках")
            return {"status": "ok", "preset": preset_name, "message": f"Пресет переключён на «{EntityExtractor.SCHEMA_PRESETS[preset_name]['name']}»"}
        
        # Режим 2: ручная схема
        entity_extractor.set_domain_schema(data, mark_manual=True)
        await asyncio.to_thread(kg_service.set_domain_schema, data.get("core", {}))
        config_store.set("kg_config", EntityExtractor.DOMAIN_ACTIVE_KEY, {
            "mode": "manual", "preset": None, "schema": data,
        })
        # Прежний ключ оставлен для совместимости (читается внешними скриптами).
        config_store.set("kg_config", "domain_schema", data)
        logger.info("[domain] ручная схема сохранена в настройках")
        return {"status": "ok", "message": "Доменная схема обновлена вручную"}
    except Exception as e:
        return {"status": "error", "message": str(e)}



# ============================================================
# Watchdog — сторож перестроения графа
# ============================================================

@router.get("/watchdog/status", summary="Статус сторожа")
async def watchdog_status(current_user: Optional[User] = Depends(get_current_user_optional)):
    try:
        from src.api.services.config_store import config_store
        status = config_store.get("kg_config", "rebuild_status") or "idle"
        stats = config_store.get("kg_config", "rebuild_stats") or {}
        return {
            "status": status,
            "entities": stats.get("entities", 0),
            "relations": stats.get("relations", 0),
            "last_update": stats.get("last_update", 0),
            "watchdog_run": stats.get("watchdog_run", 0)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}




# ============================================================
# Type Watchdog — сторож типизации документов
# ============================================================

@router.post("/type-watchdog/start", summary="Запустить сторожа типизации")
async def start_type_watchdog(current_user: User = Depends(get_current_admin)):
    try:
        from src.indexing.type_watchdog import type_watchdog
        type_watchdog.start()
        return {"status": "ok", "message": "TypeWatchdog запущен"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/type-watchdog/status", summary="Статус типизации")
async def type_watchdog_status(current_user: Optional[User] = Depends(get_current_user_optional)):
    try:
        from src.api.services.config_store import config_store
        status_raw = config_store.get("kg_config", "type_watch_status") or {}
        status = status_raw.get("state", "idle") if isinstance(status_raw, dict) else "idle"
        progress = config_store.get("kg_config", "type_watch_progress") or {}
        # Count docs without type
        from src.api.services.document_repository import get_doc_repo
        docs = await asyncio.to_thread(lambda: get_doc_repo().get_all() or {})
        total = with_type = 0
        for d in docs.values():
            if not isinstance(d, dict) or d.get("status") != "completed":
                continue
            total += 1
            dt = d.get("document_type")
            if dt and dt not in ("unknown", None, ""):
                with_type += 1
        return {
            "status": status,
            "total": total,
            "with_type": with_type,
            "without_type": total - with_type,
            "processed": progress.get("processed", 0),
            "total_progress": progress.get("total", 0)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
