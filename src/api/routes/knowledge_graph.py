"""
API-роуты для Knowledge Graph (Neo4j).
"""

from fastapi import APIRouter, HTTPException, Depends, Body
from typing import Optional
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
    """Гибридный поиск: граф + вектор."""
    try:
        from src.indexing.knowledge_graph import kg_service
        entities = [e.strip() for e in q.split(",") if e.strip()]
        doc_ids = [doc_id] if doc_id else None
        results = kg_service.hybrid_search(entities, doc_ids)
        return {"query": q, "results": results, "total": len(results)}
    except Exception as e:
        return {"query": q, "results": [], "total": 0, "error": str(e)}
