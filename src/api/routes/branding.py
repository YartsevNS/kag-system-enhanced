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
        # Оформление: настраивается в админке, применяется на всех страницах.
        # Пустое значение = цвет/шрифт темы по умолчанию (не трогаем).
        "font_body": cfg.get("font_body", ""),
        "font_display": cfg.get("font_display", ""),
        "font_size": cfg.get("font_size", 0),
        "font_weight": cfg.get("font_weight", 0),
        "contrast": cfg.get("contrast", ""),
        "color_accent": cfg.get("color_accent", ""),
        "color_bg": cfg.get("color_bg", ""),
        "color_surface": cfg.get("color_surface", ""),
        "color_text": cfg.get("color_text", ""),
    }
