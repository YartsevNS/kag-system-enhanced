"""
Модуль маршрутов API
"""

from src.api.routes import health, chat, upload, admin, auth, watchers, notifications

__all__ = ["health", "chat", "upload", "admin", "auth", "watchers", "notifications"]
