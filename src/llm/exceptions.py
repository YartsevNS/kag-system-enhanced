"""
Кастомные исключения для LLM модуля
"""

from typing import Optional


class LLMError(Exception):
    """Базовое исключение LLM"""

    def __init__(self, message: str, backend: Optional[str] = None):
        self.message = message
        self.backend = backend
        super().__init__(self.message)


class LLMConnectionError(LLMError):
    """Ошибка подключения к LLM бэкенду"""
    pass


class LLMTimeoutError(LLMError):
    """Таймаут запроса к LLM"""
    pass


class LLMRateLimitError(LLMError):
    """Превышение лимита запросов"""
    pass


class LLMAuthenticationError(LLMError):
    """Ошибка аутентификации"""
    pass


class LLMModelNotFoundError(LLMError):
    """Модель не найдена"""
    pass


class LLMInvalidRequestError(LLMError):
    """Неверный запрос"""
    pass


class LLMUnavailableError(LLMError):
    """LLM бэкенд недоступен"""
    pass


class LLMFallbackError(LLMError):
    """Ошибка fallback на другой бэкенд"""

    def __init__(self, message: str, attempted_backends: list):
        self.attempted_backends = attempted_backends
        super().__init__(message)


class NoBackendsAvailableError(LLMError):
    """Нет доступных бэкендов"""
    pass
