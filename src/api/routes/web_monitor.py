"""
API-роуты для веб-мониторинга документов.

Endpoints:
- GET  /api/v1/monitor/sources        — список источников
- POST /api/v1/monitor/sources        — добавить источник
- PUT  /api/v1/monitor/sources/{id}   — обновить источник
- DELETE /api/v1/monitor/sources/{id} — удалить источник
- POST /api/v1/monitor/check          — запустить проверку (всех или одного)
- GET  /api/v1/monitor/history        — история проверок
- GET  /api/v1/monitor/builtin        — встроенные источники
- POST /api/v1/monitor/add-builtin    — добавить все встроенные источники
"""

from fastapi import APIRouter, HTTPException, Depends, Body, Query
from typing import Optional, List
from loguru import logger
import uuid
from datetime import datetime

from src.api.middleware.auth_v2 import get_current_user_optional
from src.database.user_models import User

router = APIRouter()


# ============================================================
# Источники мониторинга (CRUD)
# ============================================================

@router.get("/sources", summary="Список источников мониторинга")
async def list_sources():
    """Получить все источники мониторинга с их статусом."""
    try:
        from src.api.services.web_monitor import web_monitor
        sources = web_monitor.get_sources()
        return {
            "total": len(sources),
            "sources": [
                {
                    "id": s.id,
                    "name": s.name,
                    "url": s.url,
                    "type": s.type,
                    "enabled": s.enabled,
                    "check_interval_minutes": s.check_interval_minutes,
                    "keywords": s.keywords,
                    "file_types": s.file_types,
                    "css_selector": s.css_selector,
                    "last_check": s.last_check.isoformat() if s.last_check else None,
                    "items_found": s.items_found,
                    "items_uploaded": s.items_uploaded,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in sources
            ]
        }
    except Exception as e:
        return {"error": str(e)}


@router.post("/sources", summary="Добавить источник мониторинга")
async def add_source(data: dict = Body(...)):
    """
    Добавить новый источник для мониторинга.
    
    Body:
    {
        "name": "Название источника",
        "url": "https://example.com/rss",
        "type": "rss|scrape|change",
        "keywords": ["ключевые", "слова"],
        "file_types": [".pdf", ".docx"],
        "css_selector": "a[href$='.pdf']",
        "check_interval_minutes": 360,
        "enabled": true
    }
    """
    try:
        from src.api.services.web_monitor import web_monitor, MonitorSource

        source = MonitorSource(
            id=str(uuid.uuid4()),
            name=data.get("name", "Без названия"),
            url=data.get("url", ""),
            type=data.get("type", "rss"),
            enabled=data.get("enabled", True),
            check_interval_minutes=data.get("check_interval_minutes", 360),
            keywords=data.get("keywords", []),
            file_types=data.get("file_types", [".pdf", ".docx"]),
            css_selector=data.get("css_selector", "a[href$='.pdf'], a[href$='.docx']"),
            batch_size=data.get("batch_size", 5),
            batch_delay=float(data.get("batch_delay", 15.0)),
            item_delay=float(data.get("item_delay", 2.0)),
        )

        if not source.url:
            raise HTTPException(status_code=400, detail="URL обязателен")
        if source.type not in ("rss", "scrape", "change"):
            raise HTTPException(status_code=400, detail=f"Неизвестный тип: {source.type}")

        web_monitor.save_source(source)
        return {"status": "ok", "source": {"id": source.id, "name": source.name}}
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.put("/sources/{source_id}", summary="Обновить источник")
async def update_source(source_id: str, data: dict = Body(...)):
    """Обновить существующий источник мониторинга."""
    try:
        from src.api.services.web_monitor import web_monitor, MonitorSource

        sources = web_monitor.get_sources()
        existing = next((s for s in sources if s.id == source_id), None)
        if not existing:
            raise HTTPException(status_code=404, detail="Источник не найден")

        # Обновляем только переданные поля
        for field in ['name', 'url', 'type', 'enabled', 'check_interval_minutes',
                       'keywords', 'file_types', 'css_selector']:
            if field in data:
                setattr(existing, field, data[field])

        web_monitor.save_source(existing)
        return {"status": "ok", "message": "Источник обновлён"}
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.delete("/sources/{source_id}", summary="Удалить источник")
async def delete_source(source_id: str):
    """Удалить источник мониторинга."""
    try:
        from src.api.services.web_monitor import web_monitor
        web_monitor.delete_source(source_id)
        return {"status": "ok", "message": "Источник удалён"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# Запуск проверки
# ============================================================

@router.post("/check", summary="Запустить проверку источников")
async def run_check(
    source_id: Optional[str] = None,
    body: dict = Body(default={})
):
    """
    Запустить проверку источников мониторинга.
    
    Если source_id не указан — проверяются все активные источники.
    Можно передать source_id в query-параметре (?source_id=...) или в теле JSON.
    """
    # Берём source_id из тела если не передан как query-параметр
    sid = source_id or body.get("source_id")
    try:
        from src.api.services.web_monitor import web_monitor
        results = await web_monitor.run_check(sid)
        return {
            "status": "ok",
            "checked": len(results),
            "results": [
                {
                    "source_id": r.source_id,
                    "status": r.status,
                    "new_items": r.new_items,
                    "skipped_items": r.skipped_items,
                    "error": r.error,
                    "checked_at": r.checked_at.isoformat()
                }
                for r in results
            ]
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# История и встроенные источники
# ============================================================

@router.get("/history", summary="История проверок")
async def get_history(limit: int = 50):
    """Получить историю последних проверок."""
    try:
        from src.api.services.web_monitor import web_monitor
        history = web_monitor.get_history(limit)
        return {"total": len(history), "history": history}
    except Exception as e:
        return {"error": str(e)}


@router.get("/builtin", summary="Встроенные источники")
async def get_builtin_sources():
    """Получить список встроенных (предустановленных) источников."""
    try:
        from src.api.services.web_monitor import WebMonitorService
        return {"sources": WebMonitorService.BUILTIN_SOURCES}
    except Exception as e:
        return {"error": str(e)}


@router.post("/add-builtin", summary="Добавить все встроенные источники")
async def add_builtin_sources():
    """Добавить все встроенные источники мониторинга (федеральные RSS-ленты)."""
    try:
        from src.api.services.web_monitor import web_monitor, MonitorSource, WebMonitorService

        existing = {s.url for s in web_monitor.get_sources()}
        added = 0

        for src in WebMonitorService.BUILTIN_SOURCES:
            if src["url"] in existing:
                continue  # Уже добавлен
            source = MonitorSource(
                id=str(uuid.uuid4()),
                name=src["name"],
                url=src["url"],
                type=src["type"],
                keywords=src.get("keywords", []),
                css_selector=src.get("css_selector", "a[href$='.pdf'], a[href$='.docx']"),
                file_types=src.get("file_types", [".pdf", ".docx"]),
            )
            web_monitor.save_source(source)
            added += 1

        return {"status": "ok", "message": f"Добавлено {added} встроенных источников"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
