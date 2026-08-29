"""
API-роуты для Knowledge Graph (Neo4j).
"""

from fastapi import APIRouter, HTTPException, Depends, Body
from typing import Optional, List
from loguru import logger

from src.api.middleware.auth_v2 import get_current_user_optional, get_current_admin
from src.database.user_models import User

router = APIRouter()


@router.post("/cypher", summary="Произвольный Cypher-запрос")
async def execute_cypher(
    query: dict = Body(...),
    current_user: User = Depends(get_current_admin)
):
    """Выполнение произвольного Cypher-запроса (только чтение)."""
    try:
        from src.indexing.knowledge_graph import kg_service
        q = query.get("query", "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="Пустой запрос")
        limit = int(query.get("limit", 100))
        results = kg_service.execute_cypher(q, limit)
        return {"query": q, "results": results, "total": len(results)}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Ошибка Cypher: {e}")
        return {"query": query.get("query"), "results": [], "error": str(e)}


@router.get("/stats", summary="Статистика графа знаний")
async def kg_stats(current_user: Optional[User] = Depends(get_current_user_optional)):
    """Статистика: количество документов, чанков, сущностей, связей."""
    try:
        from src.indexing.knowledge_graph import kg_service
        return kg_service.get_stats()
    except Exception as e:
        logger.error(f"Ошибка статистики графа: {e}")
        return {"documents": 0, "chunks": 0, "entities": 0, "relations": 0}


@router.get("/entities/search", summary="Поиск сущностей")
async def search_entities(
    q: str, 
    type: Optional[str] = None, 
    limit: int = 20,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Поиск сущностей по имени и типу."""
    try:
        from src.indexing.knowledge_graph import kg_service
        return {"results": kg_service.search_entities(q, type, limit)}
    except Exception as e:
        return {"results": [], "error": str(e)}


@router.get("/entities/{document_id}", summary="Сущности документа")
async def document_entities(
    document_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Все сущности, извлечённые из документа."""
    try:
        from src.indexing.knowledge_graph import kg_service
        entities = kg_service.get_document_entities(document_id)
        return {"document_id": document_id, "entities": entities, "total": len(entities)}
    except Exception as e:
        return {"document_id": document_id, "entities": [], "total": 0, "error": str(e)}


@router.get("/graph/{entity_name}", summary="Подграф сущности")
async def entity_graph(
    entity_name: str, 
    depth: int = 2,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Подграф вокруг сущности (узлы + связи)."""
    try:
        from src.indexing.knowledge_graph import kg_service
        return {"entity": entity_name, "graph": kg_service.get_entity_graph(entity_name, depth)}
    except Exception as e:
        return {"entity": entity_name, "graph": [], "error": str(e)}


@router.get("/hybrid-search", summary="Гибридный поиск")
async def hybrid_search(
    q: str, 
    doc_id: Optional[str] = None,
    scope: Optional[str] = "both",
    relevance_score: Optional[float] = 0.4,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Гибридный поиск: граф (Neo4j) + вектор (Qdrant).
    
    scope: "both" | "neo4j" | "qdrant"
    relevance_score: минимальный score для Qdrant-результатов (0 = без фильтра)
    """
    try:
        from src.indexing.knowledge_graph import kg_service
        from src.indexing.embeddings_service import embeddings_service
        
        entities = [e.strip() for e in q.split(",") if e.strip()]
        doc_ids = [doc_id] if doc_id else None
        results = []
        
        # 1. Поиск в графе Neo4j
        if scope != "qdrant":
            results = kg_service.hybrid_search(entities, doc_ids) if entities else []
        
        # 2. Если граф ничего не нашёл или scope=qdrant — ищем через Qdrant
        if not results or scope == "qdrant":
            try:
                await embeddings_service.initialize()
                qdrant_results = await embeddings_service.search(q, limit=20)
                seen_texts = set()
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
                    if q.lower() in content.lower():
                        score += 0.3
                    seen_texts.add(text_key)
                    results.append({
                        "chunk_id": point.get("chunk_id", ""),
                        "text": content[:500],
                        "doc_id": point.get("document_id", ""),
                        # ВАЖНО: filename из payload-поля filename, НЕ file_type!
                        # Была ошибка: point.get("file_type") — подставлялся MIME-тип
                        # (application/pdf, .txt) вместо имени файла → кракозябры.
                        "filename": point.get("filename") or point.get("file_name") or "",
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

    status = config_store.get("kg_config", "rebuild_status") or "idle"
    if status == "running":
        raise HTTPException(status_code=409, detail="Перестроение графа уже идёт")

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
        return {"status": "error", "message": str(e)}


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
        from src.indexing.knowledge_graph import kg_service
        result = kg_service.post_process_entities(document_id)
        # Также простой dedup для Community Edition
        dedup_count = kg_service.deduplicate_entities_by_name()
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
        from src.indexing.knowledge_graph import kg_service
        result = kg_service.validate_entities(document_id)
        return result
    except Exception as e:
        return {"valid": False, "error": str(e)}


@router.get("/domain-schema", summary="Доменная схема сущностей")
async def get_domain_schema():
    """Получить текущую доменную схему + список доступных пресетов."""
    try:
        from src.indexing.entity_extractor import entity_extractor
        from src.indexing.entity_extractor import EntityExtractor
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
        from src.indexing.knowledge_graph import kg_service
        from src.api.services.config_store import config_store
        
        # Режим 1: переключение пресета
        if "preset" in data:
            preset_name = data["preset"]
            result = EntityExtractor.switch_preset(preset_name)
            if "error" in result:
                return {"status": "error", "message": result["error"]}
            # Обновляем активную схему в экстракторе
            entity_extractor._domain_config = dict(EntityExtractor.SCHEMA_PRESETS[preset_name]["schema"])
            kg_service.set_domain_schema(EntityExtractor.SCHEMA_PRESETS[preset_name]["schema"].get("core", {}))
            return {"status": "ok", "preset": preset_name, "message": f"Пресет переключён на «{EntityExtractor.SCHEMA_PRESETS[preset_name]['name']}»"}
        
        # Режим 2: ручная схема
        entity_extractor.set_domain_schema(data)
        kg_service.set_domain_schema(data.get("core", {}))
        config_store.set("kg_config", "domain_schema", data)
        return {"status": "ok", "message": "Доменная схема обновлена вручную"}
    except Exception as e:
        return {"status": "error", "message": str(e)}



# ============================================================
# Watchdog — сторож перестроения графа
# ============================================================

@router.post("/watchdog/start", summary="Запустить сторожа перестроения")
async def start_watchdog(current_user: User = Depends(get_current_admin)):
    try:
        from src.indexing.rebuild_watchdog import rebuild_watchdog
        rebuild_watchdog.start()
        return {"status": "ok", "message": "Watchdog запущен"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/watchdog/stop", summary="Остановить сторожа")
async def stop_watchdog(current_user: User = Depends(get_current_admin)):
    try:
        from src.indexing.rebuild_watchdog import rebuild_watchdog
        await rebuild_watchdog.stop()
        return {"status": "ok", "message": "Watchdog остановлен"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/watchdog/status", summary="Статус сторожа")
async def watchdog_status():
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
async def start_type_watchdog():
    try:
        from src.indexing.type_watchdog import type_watchdog
        type_watchdog.start()
        return {"status": "ok", "message": "TypeWatchdog запущен"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/type-watchdog/status", summary="Статус типизации")
async def type_watchdog_status():
    try:
        from src.api.services.config_store import config_store
        status_raw = config_store.get("kg_config", "type_watch_status") or {}
        status = status_raw.get("state", "idle") if isinstance(status_raw, dict) else "idle"
        progress = config_store.get("kg_config", "type_watch_progress") or {}
        # Count docs without type
        from src.api.services.document_repository import get_doc_repo
        docs = get_doc_repo().get_all() or {}
        total = sum(1 for d in docs.values() if isinstance(d, dict) and d.get('status') == 'completed')
        with_type = sum(1 for d in docs.values() 
                       if isinstance(d, dict) and d.get('document_type') 
                       and d['document_type'] not in ('unknown', None, ''))
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
