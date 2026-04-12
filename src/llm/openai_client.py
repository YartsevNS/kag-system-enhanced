"""
Клиент для OpenAI API

Поддерживает:
- OpenAI (api.openai.com)
- Azure OpenAI
- DashScope (Qwen API, Alibaba Cloud)
- Любой OpenAI-совместимый API

Это fallback бэкенд для случаев когда локальные модели недоступны.
ВАЖНО: При использовании для 152-ФЗ данных требуется осторожность!
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


class OpenAIClient(LLMBackend):
    """
    Клиент для OpenAI-совместимых API.

    Поддерживает:
    - OpenAI (ChatGPT, GPT-4)
    - Azure OpenAI
    - DashScope/Qwen
    - Любой совместимый API

    ВАЖНО: При работе с персональными данными (152-ФЗ)
    используйте только сертифицированные облачные провайдеры!
    """

    def __init__(
        self,
        base_url: str = "https://api.openai.com",
        model: str = "gpt-4",
        api_key: str = "",
        timeout: float = 120.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        organization: Optional[str] = None,
        api_version: Optional[str] = None  # Для Azure
    ):
        """
        Инициализация OpenAI клиента.

        Args:
            base_url: URL API (для OpenAI, Azure, DashScope и т.д.)
            model: Модель по умолчанию
            api_key: API ключ (обязательно)
            timeout: Таймаут запросов
            max_retries: Максимум повторных попыток
            retry_delay: Задержка между попытками
            organization: ID организации (для OpenAI)
            api_version: Версия API (для Azure)
        """
        if not api_key:
            raise ValueError("API ключ обязателен для OpenAIClient")

        super().__init__(
            backend_type=LLMBackendType.OPENAI,
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay
        )

        self._client: Optional[httpx.AsyncClient] = None
        self._healthy: Optional[bool] = None
        self._organization = organization
        self._api_version = api_version

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент"""
        if self._client is None or self._client.is_closed:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            # Добавляем organization header если есть
            if self._organization:
                headers["OpenAI-Organization"] = self._organization

            # Azure OpenAI требует api-key header вместо Authorization
            if "azure.com" in self.base_url.lower():
                headers["api-key"] = self.api_key

            # Azure требует api-version query parameter
            url_params = {}
            if self._api_version:
                url_params["api-version"] = self._api_version

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                params=url_params,
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
            logger.info("OpenAI клиент закрыт")

    def _build_chat_payload(self, request: LLMRequest) -> dict:
        """
        Построить payload для OpenAI Chat Completions API.

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
        if request.seed is not None:
            payload["seed"] = request.seed

        return payload

    def _handle_error_response(self, response: httpx.Response):
        """
        Обработка ошибок API.

        Args:
            response: HTTP ответ

        Raises:
            Соответствующее исключение LLM
        """
        status_code = response.status_code

        try:
            error_data = response.json()
            error_message = error_data.get("error", {}).get("message", response.text)
            error_type = error_data.get("error", {}).get("type", "")
            error_code = error_data.get("error", {}).get("code", "")
        except Exception:
            error_message = response.text
            error_type = ""
            error_code = ""

        if status_code == 401:
            raise LLMAuthenticationError(
                f"Ошибка аутентификации: {error_message}",
                backend="openai"
            )
        elif status_code == 403:
            raise LLMAuthenticationError(
                f"Доступ запрещен: {error_message}",
                backend="openai"
            )
        elif status_code == 404:
            raise LLMModelNotFoundError(
                f"Модель не найдена: {error_message}",
                backend="openai"
            )
        elif status_code == 429:
            # Rate limit или quota exceeded
            if "quota" in error_message.lower():
                raise LLMRateLimitError(
                    f"Квота исчерпана: {error_message}",
                    backend="openai"
                )
            else:
                raise LLMRateLimitError(
                    f"Превышен лимит запросов: {error_message}",
                    backend="openai"
                )
        elif status_code >= 500:
            raise LLMUnavailableError(
                f"API сервер недоступен (код: {status_code})",
                backend="openai"
            )
        else:
            raise LLMInvalidRequestError(
                f"Ошибка запроса (код: {status_code}): {error_message}",
                backend="openai"
            )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Сгенерировать ответ через OpenAI API.

        Args:
            request: Запрос к LLM

        Returns:
            Ответ от LLM

        Raises:
            LLMAuthenticationError: Ошибка аутентификации
            LLMRateLimitError: Превышение лимита
            LLMModelNotFoundError: Модель не найдена
            LLMUnavailableError: API недоступен
        """
        try:
            client = await self._get_client()
            payload = self._build_chat_payload(request)

            logger.debug(
                f"OpenAI запрос: model={payload['model']}, "
                f"messages={len(payload['messages'])}, "
                f"max_tokens={payload['max_tokens']}"
            )

            response = await client.post(
                "/v1/chat/completions",
                json=payload
            )

            # Обработка ошибок
            if response.status_code != 200:
                self._handle_error_response(response)

            # Парсинг ответа
            data = response.json()

            # Преобразуем usage
            usage = None
            if "usage" in data:
                usage = UsageInfo(**data["usage"])

            return LLMResponse(
                id=data.get("id", str(uuid.uuid4())),
                model=data.get("model", self.model),
                choices=data.get("choices", []),
                usage=usage,
                backend=LLMBackendType.OPENAI,
                metadata={
                    "created": data.get("created"),
                    "system_fingerprint": data.get("system_fingerprint")
                }
            )

        except httpx.TimeoutException:
            raise LLMTimeoutError(
                f"Таймаут запроса к OpenAI ({self.timeout}с)",
                backend="openai"
            )
        except httpx.ConnectError as e:
            raise LLMConnectionError(
                f"Ошибка подключения к OpenAI ({self.base_url}): {e}",
                backend="openai"
            )
        except LLMError:
            raise
        except Exception as e:
            raise LLMConnectionError(
                f"Неожиданная ошибка OpenAI: {e}",
                backend="openai"
            )

    async def generate_stream(
        self,
        request: LLMRequest
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Потоковая генерация через OpenAI API.

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

            logger.debug(f"OpenAI потоковый запрос: model={payload['model']}")

            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json=payload
            ) as response:

                if response.status_code != 200:
                    error_text = await response.aread()
                    self._handle_error_response(response)

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
                                    backend=LLMBackendType.OPENAI
                                )
                        except json.JSONDecodeError:
                            logger.warning(f"Не удалось распарсить SSE: {data_str}")
                            continue

        except httpx.TimeoutException:
            raise LLMTimeoutError(
                f"Таймаут потокового запроса к OpenAI",
                backend="openai"
            )
        except httpx.ConnectError as e:
            raise LLMConnectionError(
                f"Ошибка подключения к OpenAI: {e}",
                backend="openai"
            )
        except LLMError:
            raise
        except Exception as e:
            raise LLMConnectionError(
                f"Неожиданная ошибка OpenAI stream: {e}",
                backend="openai"
            )

    async def health_check(self) -> LLMHealthStatus:
        """
        Проверить здоровье OpenAI API.

        Для обланых API проверяем доступность endpoint.
        """
        start_time = time.time()

        try:
            client = await self._get_client()

            # OpenAI не предоставляет отдельного health endpoint,
            # поэтому делаем минимальный запрос к models
            response = await client.get("/v1/models", timeout=10.0)
            response_time_ms = (time.time() - start_time) * 1000

            healthy = response.status_code == 200
            self._healthy = healthy

            return LLMHealthStatus(
                backend=LLMBackendType.OPENAI,
                healthy=healthy,
                model=self.model,
                response_time_ms=response_time_ms
            )

        except Exception as e:
            response_time_ms = (time.time() - start_time) * 1000
            self._healthy = False

            return LLMHealthStatus(
                backend=LLMBackendType.OPENAI,
                healthy=False,
                model=self.model,
                response_time_ms=response_time_ms,
                error=str(e)
            )

    async def get_available_models(self) -> list[str]:
        """Получить список доступных моделей"""
        try:
            client = await self._get_client()
            response = await client.get("/v1/models")

            if response.status_code == 200:
                data = response.json()
                return [m["id"] for m in data.get("data", [])]

            return []

        except Exception as e:
            logger.error(f"Ошибка получения моделей OpenAI: {e}")
            return []

    @property
    def is_available(self) -> bool:
        """Проверить доступность OpenAI API"""
        return self._healthy if self._healthy is not None else True

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()


# ===========================================
# Фабрики для конкретных провайдеров
# ===========================================

def create_openai_client(
    api_key: str,
    model: str = "gpt-4",
    **kwargs
) -> OpenAIClient:
    """
    Создать клиент для OpenAI.

    Args:
        api_key: OpenAI API ключ
        model: Модель по умолчанию
        **kwargs: Дополнительные параметры

    Returns:
        Настроенный OpenAIClient
    """
    return OpenAIClient(
        base_url="https://api.openai.com",
        model=model,
        api_key=api_key,
        **kwargs
    )


def create_azure_client(
    api_key: str,
    base_url: str,
    model: str,
    api_version: str = "2024-02-15-preview",
    **kwargs
) -> OpenAIClient:
    """
    Создать клиент для Azure OpenAI.

    Args:
        api_key: Azure API ключ
        base_url: URL ресурса (например, https://your-resource.openai.azure.com)
        model: Название deployment
        api_version: Версия API
        **kwargs: Дополнительные параметры

    Returns:
        Настроенный OpenAIClient для Azure
    """
    return OpenAIClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_version=api_version,
        **kwargs
    )


def create_dashscope_client(
    api_key: str,
    model: str = "qwen-max",
    **kwargs
) -> OpenAIClient:
    """
    Создать клиент для DashScope (Qwen API).

    DashScope предоставляет OpenAI-совместимый API.

    Args:
        api_key: DashScope API ключ
        model: Модель (qwen-turbo, qwen-plus, qwen-max)
        **kwargs: Дополнительные параметры

    Returns:
        Настроенный OpenAIClient для DashScope
    """
    return OpenAIClient(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model=model,
        api_key=api_key,
        **kwargs
    )
