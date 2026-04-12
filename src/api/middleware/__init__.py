"""
Middleware модуль для KAG API
"""

from src.api.middleware.auth import AuthMiddleware
from src.api.middleware.setup_checker import SetupCheckMiddleware

__all__ = ["AuthMiddleware", "SetupCheckMiddleware"]
