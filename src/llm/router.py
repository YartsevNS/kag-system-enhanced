"""
Роутер LLM бэкендов с автоматическим fallback

Отвечает за:
- Выбор бэкенда согласно конфигурации
- Health checks
- Автоматический fallback при ошибках
- Приоритизацию бэкендов
- Статистику и мониторинг
"""

from typing import Optional, List, Dict, Any, AsyncGenerator
import time
import asyncio
from loguru import logger
from datetime import datetime

from src.llm.base import LLMBackend
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
    LLMUnavailableError,
    LLMFallbackError,
    NoBackendsAvailableError
)


class BackendEntry:
    """Запись бэкенда в роутере"""

    def __init__(
        self,
        backend: LLMBackend,
        priority: int = 0,
        enabled: bool = True
    ):
        """
        Args:
            backend: Экземпляр бэкенда
            priority: Приоритет (0 = наивысший)
            enabled: Включен ли бэкенд
        """
        self.backend = backend
        self.priority = priority
        self.enabled = enabled
        self.consecutive_failures = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_success_time: Optional[datetime] = None
        self.circuit_open = False  # Circuit breaker flag
        self.circuit_opened_at: Optional[datetime] = None

    @property
    def is_healthy(self) -> bool:
        """Проверить здоровье бэкенда"""
        if not self.enabled:
            return False
        if self.circuit_open:
            return False
        return self.backend.is_available

    def record_success(self):
        """Записать успешный запрос"""
        self.consecutive_failures = 0
        self.last_success_time = datetime.utcnow()

        # Если circuit был открыт, закрываем
        if self.circuit_open:
            self.circuit_open = False
            self.circuit_opened_at = None
            logger.info(f"Circuit closed для {self.backend.backend_type.value}")

    def record_failure(self):
        """Записать ошибку"""
        self.consecutive_failures += 1
        self.last_failure_time = datetime.utcnow()

        # Открываем circuit после 3 последовательных ошибок
        if self.consecutive_failures >= 3 and not self.circuit_open:
            self.circuit_open = True
            self.circuit_opened_at = datetime.utcnow()
            logger.warning(
                f"Circuit opened для {self.backend.backend_type.value} "
                f"(failures={self.consecutive_failures})"
            )

    def should_attempt(self) -> bool:
        """Проверить стоит ли пытаться использовать бэкенд"""
        if not self.is_healthy:
            return False

        # Если circuit открыт, пробуем раз в 30 секунд
        if self.circuit_open and self.circuit_opened_at:
            time_since_open = (datetime.utcnow() - self.circuit_opened_at).total_seconds()
            if time_since_open < 30:
                return False
            logger.info(f"Circuit half-open, пробная попытка для {self.backend.backend_type.value}")

        return True

    def __repr__(self):
        return (
            f"BackendEntry({self.backend.backend_type.value}, "
            f"priority={self.priority}, "
            f"healthy={self.is_healthy}, "
            f"circuit_open={self.circuit_open})"
        )


class LLMRouter:
    """
    Роутер LLM бэкендов.

    Обеспечивает:
    - Приоритизацию бэкендов
    - Автоматический fallback
    - Circuit breaker паттерн
    - Health checks
    - Статистику
    """

    def __init__(
        self,
        health_check_interval: int = 60,
        auto_recovery: bool = True
    ):
        """
        Инициализация роутера.

        Args:
            health_check_interval: Интервал проверки здоровья (секунды)
            auto_recovery: Автоматическое восстановление бэкендов
        """
        self._backends: Dict[LLMBackendType, BackendEntry] = {}
        self._health_check_interval = health_check_interval
        self._auto_recovery = auto_recovery
        self._health_check_task: Optional[asyncio.Task] = None
        self._initialized = False

        logger.info(
            f"LLMRouter инициализирован: health_check={health_check_interval}с, "
            f"auto_recovery={auto_recovery}"
        )

    def add_backend(
        self,
        backend: LLMBackend,
        priority: int = 0,
        enabled: bool = True
    ):
        """
        Добавить бэкенд в роутер.

        Args:
            backend: Экземпляр LLMBackend
            priority: Приоритет (0 = наивысший)
            enabled: Начальное состояние
        """
        entry = BackendEntry(backend=backend, priority=priority, enabled=enabled)
        self._backends[backend.backend_type] = entry

        logger.info(
            f"Бэкенд добавлен: {backend.backend_type.value}, "
            f"priority={priority}, model={backend.model}"
        )

    def remove_backend(self, backend_type: LLMBackendType):
        """Удалить бэкенд из роутера"""
        if backend_type in self._backends:
            del self._backends[backend_type]
            logger.info(f"Бэкенд удален: {backend_type.value}")

    def get_backend(self, backend_type: LLMBackendType) -> Optional[LLMBackend]:
        """Получить бэкенд по типу"""
        entry = self._backends.get(backend_type)
        return entry.backend if entry else None

    def _get_sorted_backends(self) -> List[BackendEntry]:
        """Получить отсортированные по приоритету бэкенды"""
        return sorted(self._backends.values(), key=lambda x: x.priority)

    def _get_healthy_backends(self) -> List[BackendEntry]:
        """Получить здоровые бэкенды в порядке приоритета"""
        return [
            entry for entry in self._get_sorted_backends()
            if entry.should_attempt()
        ]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Сгенерировать ответ с автоматическим fallback.

        Пытается использовать бэкенды в порядке приоритета.
        При ошибке автоматически переключается на следующий.

        Args:
            request: Запрос к LLM

        Returns:
            Ответ от LLM

        Raises:
            NoBackendsAvailableError: Если нет доступных бэкендов
            LLMFallbackError: Если все бэкенды отказали
        """
        healthy_backends = self._get_healthy_backends()

        if not healthy_backends:
            raise NoBackendsAvailableError(
                "Нет доступных бэкендов. Проверьте конфигурацию и health checks."
            )

        attempted_backends = []
        last_error: Optional[LLMError] = None

        for entry in healthy_backends:
            backend = entry.backend
            backend_name = backend.backend_type.value

            try:
                logger.info(f"Попытка генерации через: {backend_name}")

                # Выполняем запрос с retry внутри бэкенда
                response = await backend.generate_with_retry(request)

                # Успех!
                entry.record_success()
                logger.info(
                    f"Успешная генерация через: {backend_name}, "
                    f"tokens={response.usage.total_tokens if response.usage else 'N/A'}"
                )

                return response

            except LLMError as e:
                last_error = e
                entry.record_failure()
                attempted_backends.append(backend_name)

                logger.warning(
                    f"Ошибка {backend_name}: {e}. "
                    f"Пробуем следующий бэкенд..."
                )

                # Если это rate limit, ждем немного перед переключением
                if isinstance(e, LLMRateLimitError):
                    await asyncio.sleep(1)

                continue

        # Все бэкенды отказали
        error_msg = (
            f"Все бэкенды отказали. Попытки: {attempted_backends}. "
            f"Последняя ошибка: {last_error}"
        )

        logger.error(error_msg)

        raise LLMFallbackError(
            message=error_msg,
            attempted_backends=attempted_backends
        )

    async def generate_stream(
        self,
        request: LLMRequest
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Потоковая генерация с fallback.

        Args:
            request: Запрос к LLM

        Yields:
            Чанки потокового ответа

        Raises:
            Те же что и generate()
        """
        healthy_backends = self._get_healthy_backends()

        if not healthy_backends:
            raise NoBackendsAvailableError("Нет доступных бэкендов")

        attempted_backends = []

        for entry in healthy_backends:
            backend = entry.backend
            backend_name = backend.backend_type.value

            try:
                logger.info(f"Потоковая генерация через: {backend_name}")

                async for chunk in backend.generate_stream(request):
                    yield chunk

                    # Если поток завершился успешно
                    if chunk.finish_reason:
                        entry.record_success()
                        logger.info(
                            f"Потоковая генерация завершена: {backend_name}"
                        )
                        return

            except LLMError as e:
                entry.record_failure()
                attempted_backends.append(backend_name)

                logger.warning(
                    f"Ошибка потока {backend_name}: {e}. "
                    f"Пробуем следующий..."
                )
                continue

        # Все бэкенды отказали
        raise LLMFallbackError(
            message=f"Все бэкенды отказали для потока: {attempted_backends}",
            attempted_backends=attempted_backends
        )

    async def health_check(self) -> Dict[LLMBackendType, LLMHealthStatus]:
        """
        Проверить здоровье всех бэкендов.

        Returns:
            Словарь {тип: статус}
        """
        results = {}

        for backend_type, entry in self._backends.items():
            try:
                status = await entry.backend.health_check()
                results[backend_type] = status

                # Если здоров, сбрасываем circuit
                if status.healthy:
                    entry.record_success()

                logger.debug(
                    f"Health check {backend_type.value}: "
                    f"healthy={status.healthy}, "
                    f"response_time={status.response_time_ms}ms"
                )

            except Exception as e:
                results[backend_type] = LLMHealthStatus(
                    backend=backend_type,
                    healthy=False,
                    model=entry.backend.model,
                    error=str(e)
                )
                entry.record_failure()

        return results

    async def start_health_monitoring(self):
        """Запустить фоновый мониторинг здоровья"""
        if self._health_check_task and not self._health_check_task.done():
            logger.warning("Мониторинг здоровья уже запущен")
            return

        async def _monitor_loop():
            """Цикл мониторинга"""
            while True:
                try:
                    await asyncio.sleep(self._health_check_interval)

                    # Проверяем здоровье
                    results = await self.health_check()

                    # Логируем изменения
                    for backend_type, status in results.items():
                        entry = self._backends.get(backend_type)
                        if entry:
                            if status.healthy and entry.circuit_open:
                                logger.info(
                                    f"Бэкенд восстановлен: {backend_type.value}"
                                )
                            elif not status.healthy and not entry.circuit_open:
                                logger.warning(
                                    f"Бэкенд стал недоступен: {backend_type.value}, "
                                    f"ошибка: {status.error}"
                                )

                except asyncio.CancelledError:
                    logger.info("Мониторинг здоровья остановлен")
                    break
                except Exception as e:
                    logger.error(f"Ошибка в мониторинге здоровья: {e}")

        self._health_check_task = asyncio.create_task(_monitor_loop())
        logger.info(
            f"Мониторинг здоровья запущен: интервал={self._health_check_interval}с"
        )

    def stop_health_monitoring(self):
        """Остановить мониторинг здоровья"""
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            logger.info("Мониторинг здоровья остановлен")

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику роутера"""
        stats = {
            "total_backends": len(self._backends),
            "healthy_backends": sum(
                1 for e in self._backends.values() if e.is_healthy
            ),
            "backends": {}
        }

        for backend_type, entry in self._backends.items():
            backend_stats = entry.backend.get_stats()
            stats["backends"][backend_type.value] = {
                "stats": backend_stats.model_dump(),
                "priority": entry.priority,
                "enabled": entry.enabled,
                "circuit_open": entry.circuit_open,
                "consecutive_failures": entry.consecutive_failures
            }

        return stats

    def get_primary_backend(self) -> Optional[LLMBackendType]:
        """Получить основной (здоровый с наивысшим приоритетом) бэкенд"""
        healthy = self._get_healthy_backends()
        return healthy[0].backend.backend_type if healthy else None

    def enable_backend(self, backend_type: LLMBackendType) -> bool:
        """Включить бэкенд"""
        entry = self._backends.get(backend_type)
        if entry:
            entry.enabled = True
            logger.info(f"Бэкенд включен: {backend_type.value}")
            return True
        return False

    def disable_backend(self, backend_type: LLMBackendType) -> bool:
        """Отключить бэкенд"""
        entry = self._backends.get(backend_type)
        if entry:
            entry.enabled = False
            entry.circuit_open = True
            logger.info(f"Бэкенд отключен: {backend_type.value}")
            return True
        return False

    async def __aenter__(self):
        """Async context manager entry"""
        await self.start_health_monitoring()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        self.stop_health_monitoring()

        # Закрываем все бэкенды
        for entry in self._backends.values():
            if hasattr(entry.backend, 'close'):
                await entry.backend.close()


# ===========================================
# Фабрика роутера из конфигурации
# ===========================================

def create_router_from_config(config: Dict[str, Any]) -> LLMRouter:
    """
    Создать роутер из конфигурационного словаря.

    Ожидаемый формат config:
    ```python
    {
        "backends": [
            {
                "type": "vllm",
                "base_url": "http://vllm:8000",
                "model": "mistralai/Mistral-7B",
                "priority": 0,
                "enabled": True
            },
            {
                "type": "ollama",
                "base_url": "http://ollama:11434",
                "model": "mistral:7b",
                "priority": 1,
                "enabled": True
            },
            {
                "type": "openai",
                "base_url": "https://api.openai.com",
                "model": "gpt-4",
                "api_key": "sk-...",
                "priority": 2,
                "enabled": False
            }
        ],
        "health_check_interval": 60,
        "auto_recovery": True
    }
    ```

    Args:
        config: Конфигурация роутера

    Returns:
        Настроенный LLMRouter
    """
    from src.llm.vllm_client import VLLMClient
    from src.llm.ollama_client import OllamaClient
    from src.llm.openai_client import OpenAIClient

    router = LLMRouter(
        health_check_interval=config.get("health_check_interval", 60),
        auto_recovery=config.get("auto_recovery", True)
    )

    for backend_config in config.get("backends", []):
        backend_type = backend_config.get("type")
        priority = backend_config.get("priority", 0)
        enabled = backend_config.get("enabled", True)

        # Создаем бэкенд соответствующего типа
        if backend_type == "vllm":
            backend = VLLMClient(
                base_url=backend_config["base_url"],
                model=backend_config["model"],
                api_key=backend_config.get("api_key"),
                timeout=backend_config.get("timeout", 120.0)
            )
        elif backend_type == "ollama":
            backend = OllamaClient(
                base_url=backend_config.get("base_url", "http://localhost:11434"),
                model=backend_config.get("model", "mistral:7b"),
                timeout=backend_config.get("timeout", 120.0)
            )
        elif backend_type == "openai":
            backend = OpenAIClient(
                base_url=backend_config.get("base_url", "https://api.openai.com"),
                model=backend_config["model"],
                api_key=backend_config["api_key"],
                timeout=backend_config.get("timeout", 120.0)
            )
        else:
            logger.warning(f"Неизвестный тип бэкенда: {backend_type}")
            continue

        router.add_backend(backend, priority=priority, enabled=enabled)

    logger.info(
        f"Роутер создан из конфигурации: "
        f"{len(router._backends)} бэкендов"
    )

    return router
