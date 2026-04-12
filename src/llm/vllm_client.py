"""
Клиент для vLLM сервера

vLLM предоставляет OpenAI-совместимый API, что упрощает интеграцию.
https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html
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


class VLLMClient(LLMBackend):
    """
    Клиент для vLLM сервера.

    vLLM - высокопроизводительный сервер для LLM с поддержкой:
    - Continuous batching
    - PagedAttention
    - OpenAI-совместимый API
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        retry_delay: float = 1.0
    ):
        """
        Инициализация vLLM клиента.

        Args:
            base_url: URL vLLM сервера (например, http://vllm:8000)
            model: Модель по умолчанию
            api_key: API ключ (опционально)
            timeout: Таймаут запросов
            max_retries: Максимум повторных попыток
            retry_delay: Задержка между попытками
        """
        super().__init__(
            backend_type=LLMBackendType.VLLM,
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay
        )

        self._client: Optional[httpx.AsyncClient] = None
        self._healthy: Optional[bool] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент"""
        if self._client is None or self._client.is_closed:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
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
            logger.info("vLLM клиент закрыт")

    def _build_chat_payload(self, request: LLMRequest) -> dict:
        """
        Построить payload для OpenAI-совместимого API.

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

        payload = {
            "model": request.model or self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "top_p": request.top_p,
            "stream": request.stream,
            "presence_penalty": request.presence_penalty,
            "frequency_penalty": request.frequency_penalty,
        }

        # Опциональные параметры
        if request.stop:
            payload["stop"] = request.stop
        if request.top_k > 0:
            payload["top_k"] = request.top_k
        if request.seed is not None:
            payload["seed"] = request.seed

        return payload

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Сгенерировать ответ через vLLM.

        Args:
            request: Запрос к LLM

        Returns:
            Ответ от LLM

        Raises:
            LLMConnectionError: Ошибка подключения
            LLMTimeoutError: Таймаут
            LLMRateLimitError: Превышение лимита
            LLMModelNotFoundError: Модель не найдена
            LLMUnavailableError: Бэкенд недоступен
        """
        try:
            client = await self._get_client()
            payload = self._build_chat_payload(request)

            logger.debug(
                f"vLLM запрос: model={payload['model']}, "
                f"messages={len(payload['messages'])}, "
                f"max_tokens={payload['max_tokens']}"
            )

            response = await client.post(
                "/v1/chat/completions",
                json=payload
            )

            # Обработка ошибок
            if response.status_code == 401:
                raise LLMAuthenticationError(
                    "Ошибка аутентификации vLLM",
                    backend="vllm"
                )
            elif response.status_code == 429:
                raise LLMRateLimitError(
                    "Превышен лимит запросов vLLM",
                    backend="vllm"
                )
            elif response.status_code == 404:
                raise LLMModelNotFoundError(
                    f"Модель не найдена: {payload['model']}",
                    backend="vllm"
                )
            elif response.status_code >= 500:
                raise LLMUnavailableError(
                    f"vLLM сервер недоступна (код: {response.status_code})",
                    backend="vllm"
                )
            elif response.status_code != 200:
                raise LLMInvalidRequestError(
                    f"Ошибка запроса (код: {response.status_code}): {response.text}",
                    backend="vllm"
                )

            # Парсинг ответа
            data = response.json()

            # Преобразуем usage
            usage = None
            if "usage" in data:
                usage = UsageInfo(
                    prompt_tokens=data["usage"].get("prompt_tokens", 0),
                    completion_tokens=data["usage"].get("completion_tokens", 0),
                    total_tokens=data["usage"].get("total_tokens", 0)
                )

            return LLMResponse(
                id=data.get("id", str(uuid.uuid4())),
                model=data.get("model", self.model),
                choices=data.get("choices", []),
                usage=usage,
                backend=LLMBackendType.VLLM,
                metadata={
                    "created": data.get("created"),
                    "system_fingerprint": data.get("system_fingerprint")
                }
            )

        except httpx.TimeoutException:
            raise LLMTimeoutError(
                f"Таймаут запроса к vLLM ({self.timeout}с)",
                backend="vllm"
            )
        except httpx.ConnectError as e:
            raise LLMConnectionError(
                f"Ошибка подключения к vLLM ({self.base_url}): {e}",
                backend="vllm"
            )
        except LLMError:
            raise
        except Exception as e:
            raise LLMConnectionError(
                f"Неожиданная ошибка vLLM: {e}",
                backend="vllm"
            )

    async def generate_stream(
        self,
        request: LLMRequest
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Потоковая генерация через vLLM.

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

            logger.debug(f"vLLM потоковый запрос: model={payload['model']}")

            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json=payload
            ) as response:

                if response.status_code != 200:
                    error_text = await response.aread()
                    raise LLMInvalidRequestError(
                        f"Ошибка потокового запроса (код: {response.status_code}): {error_text}",
                        backend="vllm"
                    )

                # Читаем SSE поток
                async for line in response.aiter_lines():
                    if not line:
                        continue

                    # SSE формат: data: {...}
                    if line.startswith("data: "):
                        data_str = line[6:]

                        if data_str.strip() == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)

                            # Извлекаем дельту
                            if data.get("choices"):
                                choice = data["choices"][0]
                                delta = choice.get("delta", {})
                                content = delta.get("content", "")
                                finish_reason = choice.get("finish_reason")

                                yield StreamChunk(
                                    id=data.get("id", ""),
                                    model=data.get("model", self.model),
                                    delta=content,
                                    finish_reason=finish_reason,
                                    backend=LLMBackendType.VLLM
                                )
                        except json.JSONDecodeError:
                            logger.warning(f"Не удалось распарсить SSE: {data_str}")
                            continue

        except httpx.TimeoutException:
            raise LLMTimeoutError(
                f"Таймаут потокового запроса к vLLM",
                backend="vllm"
            )
        except httpx.ConnectError as e:
            raise LLMConnectionError(
                f"Ошибка подключения к vLLM: {e}",
                backend="vllm"
            )
        except LLMError:
            raise
        except Exception as e:
            raise LLMConnectionError(
                f"Неожиданная ошибка vLLM stream: {e}",
                backend="vllm"
            )

    async def health_check(self) -> LLMHealthStatus:
        """Проверить здоровье vLLM сервера"""
        start_time = time.time()

        try:
            client = await self._get_client()

            # vLLM предоставляет endpoint /health
            response = await client.get("/health")
            response_time_ms = (time.time() - start_time) * 1000

            healthy = response.status_code == 200
            self._healthy = healthy

            # Получаем информацию о модели
            models = []
            try:
                models_response = await client.get("/v1/models")
                if models_response.status_code == 200:
                    models_data = models_response.json()
                    models = [m["id"] for m in models_data.get("data", [])]
            except Exception:
                pass

            return LLMHealthStatus(
                backend=LLMBackendType.VLLM,
                healthy=healthy,
                model=models[0] if models else self.model,
                response_time_ms=response_time_ms
            )

        except Exception as e:
            response_time_ms = (time.time() - start_time) * 1000
            self._healthy = False

            return LLMHealthStatus(
                backend=LLMBackendType.VLLM,
                healthy=False,
                model=self.model,
                response_time_ms=response_time_ms,
                error=str(e)
            )

    async def get_available_models(self) -> list[str]:
        """Получить список доступных моделей vLLM"""
        try:
            client = await self._get_client()
            response = await client.get("/v1/models")

            if response.status_code == 200:
                data = response.json()
                return [m["id"] for m in data.get("data", [])]

            return []

        except Exception as e:
            logger.error(f"Ошибка получения моделей vLLM: {e}")
            return []

    @property
    def is_available(self) -> bool:
        """Проверить доступность vLLM"""
        return self._healthy if self._healthy is not None else True

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
