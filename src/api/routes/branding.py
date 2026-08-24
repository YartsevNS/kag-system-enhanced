"""
Брендинг: единое название/версия/подпись для всех страниц.

GET /api/v1/branding — публичный (без auth), отдаёт настройки из
config_store "system"/"branding". Редактируется в админке
(POST /api/v1/admin/models/branding-config).
"""

from fastapi import APIRouter
from loguru import logger

router = APIRouter(prefix="/branding", tags=["branding"])


@router.get("")
async def get_branding():
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("system", "branding") or {}
    except Exception as e:
        logger.debug(f"branding не прочитан: {e}")
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "name": cfg.get("name", "KAG"),
        "version": cfg.get("version", ""),
        "footer": cfg.get("footer", ""),
    }
