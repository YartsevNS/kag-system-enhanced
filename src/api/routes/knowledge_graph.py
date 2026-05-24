"""
API-роуты для Knowledge Graph (Neo4j).
"""

from fastapi import APIRouter, HTTPException, Depends, Body
from typing import Optional, List
from loguru import logger

from src.api.middleware.auth_v2 import get_current_user_optional
from src.database.user_models import User

router = APIRouter()


@router.post("/cypher", summary="Произвольный Cypher-запрос")
async def execute_cypher(
    query: dict = Body(...),
    current_user: Optional[User] = Depends(get_current_user_optional)
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
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """Гибридный поиск: граф (Neo4j) + вектор (Qdrant).
    
    Если сущности не найдены в графе — ищет похожие чанки через Qdrant.
    """
    try:
        from src.indexing.knowledge_graph import kg_service
        from src.indexing.embeddings_service import embeddings_service
        
        entities = [e.strip() for e in q.split(",") if e.strip()]
        doc_ids = [doc_id] if doc_id else None
        
        # 1. Поиск в графе Neo4j
        results = kg_service.hybrid_search(entities, doc_ids) if entities else []
        
        # 2. Если граф ничего не нашёл — ищем через Qdrant (векторный поиск по смыслу)
        if not results:
            try:
                await embeddings_service.initialize()
                qdrant_results = await embeddings_service.search(q, limit=20)
                seen_texts = set()
                for point in (qdrant_results or []):
                    score = point.get("score", 0)
                    content = (point.get("content", "") or "").strip()
                    # Фильтр: score > 0.35, нет дубликатов
                    if score < 0.35 or not content:
                        continue
                    text_key = content[:100]
                    if text_key in seen_texts:
                        continue
                    # Буст: если query встречается в тексте — повышаем приоритет
                    if q.lower() in content.lower():
                        score += 0.3
                    seen_texts.add(text_key)
                    results.append({
                        "chunk_id": point.get("chunk_id", ""),
                        "text": content[:500],
                        "doc_id": point.get("document_id", ""),
                        "filename": point.get("file_type", ""),
                        "score": round(score, 4),
                        "entity_count": 0,
                        "source": "qdrant"
                    })
                # Сортируем по score
                results.sort(key=lambda r: r.get("score", 0), reverse=True)
                # Обрезаем до 10 лучших
                results = results[:10]
            except Exception as e:
                logger.warning(f"Qdrant fallback failed: {e}")
        
        return {"query": q, "results": results, "total": len(results)}
    except Exception as e:
        return {"query": q, "results": [], "total": 0, "error": str(e)}


@router.post("/rebuild-graph", summary="Перестроить граф для существующих документов")
async def rebuild_graph(
    document_ids: Optional[List[str]] = Body(None, embed=True),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Переизвлечь сущности и перестроить граф для указанных документов.
    Если document_ids=None — обработать все документы со статусом completed.
    """
    try:
        from src.indexing.knowledge_graph import kg_service
        from src.indexing.entity_extractor import entity_extractor
        from src.api.services.config_store import config_store
        from src.indexing.embeddings_service import embeddings_service

        if document_ids:
            docs = []
            for did in document_ids:
                doc = config_store.get("documents", did)
                if doc:
                    docs.append(doc)
        else:
            all_docs = config_store.get_all("documents") or {}
            docs = []
            for did, doc in all_docs.items():
                if isinstance(doc, dict) and doc.get("status") == "completed":
                    doc["document_id"] = did
                    docs.append(doc)

        results = []
        for doc in docs:
            doc_id = doc.get("document_id") or doc.get("id")
            filename = doc.get("filename", "unknown")
            
            # Очищаем старые данные графа
            kg_service.clear_document(doc_id)
            
            # Создаём узел документа
            kg_service.create_document_node(doc_id, filename)
            
            # Получаем чанки из Qdrant
            chunks = await embeddings_service.get_document_chunks(doc_id)
            if not chunks:
                results.append({"document_id": doc_id, "status": "no_chunks"})
                continue
            
            # Переизвлекаем сущности
            entity_count = 0
            for i, chunk in enumerate(chunks[:10]):
                chunk_id = chunk.get("chunk_id", f"chunk_{i}")
                chunk_text = chunk.get("content", "")
                chunk_seq = chunk.get("metadata", {}).get("chunk_seq", i + 1)
                
                kg_service.create_chunk_node(chunk_id, doc_id, chunk_text, chunk_seq)
                await entity_extractor.extract_and_store(doc_id, chunk_id, chunk_text, chunk_seq, filename)
                
                # Считаем сущности после каждого чанка
                stats = kg_service.get_stats()
                entity_count = stats.get("entities", 0)
            
            results.append({
                "document_id": doc_id,
                "filename": filename,
                "chunks_processed": min(len(chunks), 10),
                "entities_found": entity_count
            })
        
        total_stats = kg_service.get_stats()
        return {"status": "ok", "results": results, "total_stats": total_stats}
    except Exception as e:
        logger.error(f"Rebuild graph error: {e}")
        return {"status": "error", "message": str(e)}


# ============================================================
# Пост-обработка и валидация (Neo4j Best Practices)
# ============================================================

@router.post("/post-process", summary="Пост-обработка графа")
async def post_process_graph(document_id: Optional[str] = None):
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
async def stop_rebuild():
    """Установить флаг остановки перестроения графа знаний."""
    try:
        from src.api.services.config_store import config_store
        config_store.set("kg_config", "rebuild_stop", True)
        return {"status": "ok", "message": "Сигнал остановки отправлен. Текущий документ будет последним."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/validate/{document_id}", summary="Валидация сущностей документа")
async def validate_document_entities(document_id: str):
    """Проверить качество извлечённых сущностей."""
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
async def update_domain_schema(data: dict):
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
async def start_watchdog():
    try:
        from src.indexing.rebuild_watchdog import rebuild_watchdog
        rebuild_watchdog.start()
        return {"status": "ok", "message": "Watchdog запущен"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/watchdog/stop", summary="Остановить сторожа")
async def stop_watchdog():
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
        docs = config_store.get_all("documents") or {}
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
