"""
Клиент для Ollama сервера

Ollama предоставляет REST API для работы с локальными LLM.
https://github.com/ollama/ollama/blob/main/docs/api.md
"""

from typing import AsyncGenerator, Optional
import json
import time
import httpx
from loguru import logger

from src.llm.base import LLMBackend
from src.llm.models import (
    LLMRequest,
    LLMResponse,
    StreamChunk,
    LLMHealthStatus,
    LLMBackendType,
    ChatMessage,
    UsageInfo
)
from src.llm.exceptions import (
    LLMConnectionError,
    LLMTimeoutError,
    LLMRateLimitError,
    LLMAuthenticationError,
    LLMModelNotFoundError,
    LLMInvalidRequestError,
    LLMUnavailableError,
    LLMError
)

import uuid
from datetime import datetime


class OllamaClient(LLMBackend):
    """
    Клиент для Ollama сервера.

    Ollama - простой способ запускать LLM локально.
    Поддерживает множество моделей из официального репозитория.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "mistral:7b",
        timeout: float = 120.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        keep_alive: str = "5m"
    ):
        """
        Инициализация Ollama клиента.

        Args:
            base_url: URL Ollama сервера
            model: Модель по умолчанию
            timeout: Таймаут запросов
            max_retries: Максимум повторных попыток
            retry_delay: Задержка между попытками
            keep_alive: Время жизни модели в памяти (например, "5m", "-1" для бесконечности)
        """
        super().__init__(
            backend_type=LLMBackendType.OLLAMA,
            base_url=base_url,
            model=model,
            api_key=None,  # Ollama не требует API ключ
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay
        )

        self._client: Optional[httpx.AsyncClient] = None
        self._healthy: Optional[bool] = None
        self.keep_alive = keep_alive

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=self.timeout,
                    write=self.timeout,
                    pool=self.timeout
                ),
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20
                )
            )

        return self._client

    async def close(self):
        """Закрыть HTTP клиент"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("Ollama клиент закрыт")

    def _build_chat_payload(self, request: LLMRequest) -> dict:
        """
        Построить payload для Ollama API.

        Ollama использует свой формат отличный от OpenAI.
        https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-chat-completion

        Args:
            request: Запрос к LLM

        Returns:
            Словарь для отправки
        """
        messages = [
            {
                "role": msg.role.value,
                "content": msg.content
            }
            for msg in request.messages
        ]

        # Ollama options
        options = {
            "temperature": request.temperature,
            "top_p": request.top_p,
            "num_predict": request.max_tokens,
        }

        if request.stop:
            options["stop"] = request.stop
        if request.seed is not None:
            options["seed"] = request.seed

        payload = {
            "model": request.model or self.model,
            "messages": messages,
            "stream": request.stream,
            "options": options,
            "keep_alive": self.keep_alive
        }

        return payload

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Сгенерировать ответ через Ollama.

        Args:
            request: Запрос к LLM

        Returns:
            Ответ от LLM

        Raises:
            LLMConnectionError: Ошибка подключения
            LLMTimeoutError: Таймаут
            LLMModelNotFoundError: Модель не найдена
            LLMUnavailableError: Бэкенд недоступен
        """
        try:
            client = await self._get_client()
            payload = self._build_chat_payload(request)

            logger.debug(
                f"Ollama запрос: model={payload['model']}, "
                f"messages={len(payload['messages'])}"
            )

            response = await client.post(
                "/api/chat",
                json=payload
            )

            # Обработка ошибок
            if response.status_code == 404:
                raise LLMModelNotFoundError(
                    f"Модель не найдена: {payload['model']}. "
                    f"Попробуйте: ollama pull {payload['model']}",
                    backend="ollama"
                )
            elif response.status_code >= 500:
                raise LLMUnavailableError(
                    f"Ollama сервер недоступен (код: {response.status_code})",
                    backend="ollama"
                )
            elif response.status_code != 200:
                raise LLMInvalidRequestError(
                    f"Ошибка запроса (код: {response.status_code}): {response.text}",
                    backend="ollama"
                )

            # Парсинг ответа
            data = response.json()

            # Ollama возвращает ответ в другом формате
            message = data.get("message", {})
            content = message.get("content", "")

            # Оценка использования (если доступна)
            usage = None
            if "total_duration" in data:
                # Ollama предоставляет метрики производительности
                usage = UsageInfo(
                    prompt_tokens=data.get("prompt_eval_count", 0),
                    completion_tokens=data.get("eval_count", 0),
                    total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
                )

            return LLMResponse(
                id=str(uuid.uuid4()),
                model=data.get("model", self.model),
                choices=[
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": content
                        },
                        "finish_reason": "stop" if data.get("done") else "length"
                    }
                ],
                usage=usage,
                backend=LLMBackendType.OLLAMA,
                metadata={
                    "created_at": data.get("created_at"),
                    "done": data.get("done"),
                    "total_duration": data.get("total_duration"),
                    "load_duration": data.get("load_duration"),
                    "eval_duration": data.get("eval_duration")
                }
            )

        except httpx.TimeoutException:
            raise LLMTimeoutError(
                f"Таймаут запроса к Ollama ({self.timeout}с)",
                backend="ollama"
            )
        except httpx.ConnectError as e:
            raise LLMConnectionError(
                f"Ошибка подключения к Ollama ({self.base_url}): {e}",
                backend="ollama"
            )
        except LLMError:
            raise
        except Exception as e:
            raise LLMConnectionError(
                f"Неожиданная ошибка Ollama: {e}",
                backend="ollama"
            )

    async def generate_stream(
        self,
        request: LLMRequest
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Потоковая генерация через Ollama.

        Args:
            request: Запрос к LLM

        Yields:
            Чанки потокового ответа
        """
        try:
            client = await self._get_client()

            # Убеждаемся что stream включен
            request.stream = True
            payload = self._build_chat_payload(request)

            logger.debug(f"Ollama потоковый запрос: model={payload['model']}")

            # Ollama возвращает JSON Newline Delimited формат
            async with client.stream(
                "POST",
                "/api/chat",
                json=payload
            ) as response:

                if response.status_code != 200:
                    error_text = await response.aread()
                    raise LLMInvalidRequestError(
                        f"Ошибка потокового запроса (код: {response.status_code}): {error_text}",
                        backend="ollama"
                    )

                response_id = str(uuid.uuid4())

                # Читаем NDJSON поток
                async for line in response.aiter_lines():
                    if not line:
                        continue

                    try:
                        data = json.loads(line)

                        # Извлекаем контент
                        message = data.get("message", {})
                        content = message.get("content", "")
                        done = data.get("done", False)

                        # Пропускаем пустые чанки
                        if not content and not done:
                            continue

                        yield StreamChunk(
                            id=response_id,
                            model=data.get("model", self.model),
                            delta=content,
                            finish_reason="stop" if done else None,
                            backend=LLMBackendType.OLLAMA
                        )

                        if done:
                            break

                    except json.JSONDecodeError:
                        logger.warning(f"Не удалось распарсить NDJSON: {line}")
                        continue

        except httpx.TimeoutException:
            raise LLMTimeoutError(
                f"Таймаут потокового запроса к Ollama",
                backend="ollama"
            )
        except httpx.ConnectError as e:
            raise LLMConnectionError(
                f"Ошибка подключения к Ollama: {e}",
                backend="ollama"
            )
        except LLMError:
            raise
        except Exception as e:
            raise LLMConnectionError(
                f"Неожиданная ошибка Ollama stream: {e}",
                backend="ollama"
            )

    async def health_check(self) -> LLMHealthStatus:
        """Проверить здоровье Ollama сервера"""
        start_time = time.time()

        try:
            client = await self._get_client()

            # Ollama root endpoint отвечает OK
            response = await client.get("/")
            response_time_ms = (time.time() - start_time) * 1000

            healthy = response.status_code == 200
            self._healthy = healthy

            return LLMHealthStatus(
                backend=LLMBackendType.OLLAMA,
                healthy=healthy,
                model=self.model,
                response_time_ms=response_time_ms
            )

        except Exception as e:
            response_time_ms = (time.time() - start_time) * 1000
            self._healthy = False

            return LLMHealthStatus(
                backend=LLMBackendType.OLLAMA,
                healthy=False,
                model=self.model,
                response_time_ms=response_time_ms,
                error=str(e)
            )

    async def get_available_models(self) -> list[str]:
        """Получить список загруженных моделей Ollama"""
        try:
            client = await self._get_client()
            response = await client.get("/api/tags")

            if response.status_code == 200:
                data = response.json()
                return [m["name"] for m in data.get("models", [])]

            return []

        except Exception as e:
            logger.error(f"Ошибка получения моделей Ollama: {e}")
            return []

    async def pull_model(self, model_name: str, stream: bool = False):
        """
        Загрузить модель с Ollama registry.

        Args:
            model_name: Название модели
            stream: Потоковая загрузка
        """
        try:
            client = await self._get_client()
            response = await client.post(
                "/api/pull",
                json={"name": model_name, "stream": stream}
            )

            if response.status_code == 200:
                logger.info(f"Модель загружена: {model_name}")
            else:
                logger.error(f"Ошибка загрузки модели: {response.text}")

        except Exception as e:
            logger.error(f"Ошибка загрузки модели {model_name}: {e}")

    @property
    def is_available(self) -> bool:
        """Проверить доступность Ollama"""
        return self._healthy if self._healthy is not None else True

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
