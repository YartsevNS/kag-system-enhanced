"""
Абстрактный базовый класс для LLM бэкендов

Определяет контракт который должны реализовать все бэкенды.
"""

from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional
import asyncio
import time
from loguru import logger

from src.llm.models import (
    LLMRequest,
    LLMResponse,
    StreamChunk,
    LLMHealthStatus,
    LLMBackendType,
    LLMStats
)
from src.llm.exceptions import (
    LLMError,
    LLMConnectionError,
    LLMTimeoutError,
    LLMRateLimitError,
    LLMAuthenticationError,
    LLMModelNotFoundError,
    LLMInvalidRequestError,
    LLMUnavailableError
)


class LLMBackend(ABC):
    """
    Абстрактный базовый класс для LLM бэкендов.

    Все реализации (vLLM, Ollama, OpenAI) должны наследовать этот класс
    и реализовать все абстрактные методы.
    """

    def __init__(
        self,
        backend_type: LLMBackendType,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        retry_delay: float = 1.0
    ):
        """
        Инициализация бэкенда.

        Args:
            backend_type: Тип бэкенда
            base_url: URL бэкенда
            model: Модель по умолчанию
            api_key: API ключ (если требуется)
            timeout: Таймаут запросов в секундах
            max_retries: Максимум повторных попыток
            retry_delay: Задержка между попытками
        """
        self.backend_type = backend_type
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        # Статистика
        self._stats = LLMStats(backend=backend_type)
        self._start_time = time.time()

        logger.info(
            f"LLMBackend инициализирован: {backend_type.value}, "
            f"model={model}, base_url={base_url}"
        )

    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Сгенерировать ответ на запрос.

        Args:
            request: Запрос к LLM

        Returns:
            Ответ от LLM

        Raises:
            LLMConnectionError: Ошибка подключения
            LLMTimeoutError: Таймаут
            LLMRateLimitError: Превышение лимита
            LLMAuthenticationError: Ошибка аутентификации
            LLMModelNotFoundError: Модель не найдена
            LLMInvalidRequestError: Неверный запрос
            LLMUnavailableError: Бэкенд недоступен
        """
        pass

    @abstractmethod
    async def generate_stream(
        self,
        request: LLMRequest
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Потоковая генерация ответа.

        Args:
            request: Запрос к LLM

        Yields:
            Чанки потокового ответа

        Raises:
            Те же что и generate()
        """
        pass

    @abstractmethod
    async def health_check(self) -> LLMHealthStatus:
        """
        Проверить здоровье бэкенда.

        Returns:
            Статус здоровья
        """
        pass

    @abstractmethod
    async def get_available_models(self) -> list[str]:
        """
        Получить список доступных моделей.

        Returns:
            Список названий моделей
        """
        pass

    async def generate_with_retry(self, request: LLMRequest) -> LLMResponse:
        """
        Сгенерировать ответ с автоматическими повторными попытками.

        Args:
            request: Запрос к LLM

        Returns:
            Ответ от LLM

        Raises:
            LLMError: Если все попытки неудачны
        """
        last_error = None

        for attempt in range(self.max_retries):
            try:
                start_time = time.time()
                response = await self.generate(request)
                duration_ms = (time.time() - start_time) * 1000

                # Обновляем статистику
                self._stats.total_requests += 1
                self._stats.successful_requests += 1
                if response.usage:
                    self._stats.total_tokens += response.usage.total_tokens
                self._stats.avg_response_time_ms = (
                    (self._stats.avg_response_time_ms * (self._stats.successful_requests - 1) + duration_ms)
                    / self._stats.successful_requests
                )

                logger.debug(
                    f"Генерация успешна: {self.backend_type.value}, "
                    f"attempt={attempt+1}, time={duration_ms:.0f}ms"
                )
                return response

            except LLMRateLimitError as e:
                last_error = e
                logger.warning(
                    f"Rate limit для {self.backend_type.value}, "
                    f"attempt={attempt+1}/{self.max_retries}"
                )
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (2 ** attempt)
                    await asyncio.sleep(wait_time)
                continue

            except LLMConnectionError as e:
                last_error = e
                logger.warning(
                    f"Ошибка подключения {self.backend_type.value}, "
                    f"attempt={attempt+1}/{self.max_retries}"
                )
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (2 ** attempt)
                    await asyncio.sleep(wait_time)
                continue

            except LLMError as e:
                # Остальные ошибки не retry'им
                self._stats.total_requests += 1
                self._stats.failed_requests += 1
                self._stats.last_error = str(e)
                logger.error(f"LLM ошибка (без retry): {e}")
                raise

        # Все попытки исчерпаны
        self._stats.total_requests += 1
        self._stats.failed_requests += 1
        self._stats.last_error = str(last_error)

        logger.error(
            f"Все {self.max_retries} попыток исчерпаны для "
            f"{self.backend_type.value}"
        )
        raise last_error

    def get_stats(self) -> LLMStats:
        """Получить статистику бэкенда"""
        self._stats.uptime_seconds = time.time() - self._start_time
        return self._stats

    def reset_stats(self):
        """Сбросить статистику"""
        self._stats = LLMStats(backend=self.backend_type)
        self._start_time = time.time()
        logger.info(f"Статистика сброшена: {self.backend_type.value}")

    @property
    def is_available(self) -> bool:
        """Проверить доступность бэкенда (кэш последней проверки)"""
        # По умолчанию True, конкретная реализация может переопределить
        return True

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
            f"backend={self.backend_type.value}, "
            f"model={self.model}, "
            f"base_url={self.base_url})"
        )
