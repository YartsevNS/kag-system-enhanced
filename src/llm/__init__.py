"""
LLM модуль для системы KAG

Поддерживает три бэкенда:
- vLLM (локальный сервер, production, 152-ФЗ compliant)
- Ollama (локальный сервер, development/testing)
- OpenAI API (внешний API, fallback/dev)

Архитектура:
- Абстрактный интерфейс LLMBackend
- Конкретные реализации для каждого бэкенда
- Роутер с автоматическим fallback и health checks

Пример использования:

    from src.llm import create_router_from_config, LLMRequest, ChatMessage, MessageRole

    # Создаем роутер из конфигурации
    config = {
        "backends": [
            {
                "type": "vllm",
                "base_url": "http://vllm:8000",
                "model": "mistralai/Mistral-7B-Instruct-v0.2",
                "priority": 0
            },
            {
                "type": "ollama",
                "base_url": "http://ollama:11434",
                "model": "mistral:7b",
                "priority": 1
            }
        ]
    }

    router = create_router_from_config(config)

    # Генерация ответа
    request = LLMRequest(
        messages=[ChatMessage(role=MessageRole.USER, content="Привет!")]
    )
    response = await router.generate(request)
    print(response.generated_text)
"""

# Модели
from src.llm.models import (
    MessageRole,
    ChatMessage,
    LLMBackendType,
    LLMRequest,
    LLMResponse,
    StreamChunk,
    LLMHealthStatus,
    LLMStats,
    UsageInfo
)

# Базовый класс
from src.llm.base import LLMBackend

# Клиенты
from src.llm.vllm_client import VLLMClient
from src.llm.ollama_client import OllamaClient
from src.llm.openai_client import (
    OpenAIClient,
    create_openai_client,
    create_azure_client,
    create_dashscope_client
)

# Embeddings
from src.llm.embeddings import (
    EmbeddingClient,
    EmbeddingResponse,
    cosine_similarity,
    normalize_vector
)

# Роутер
from src.llm.router import LLMRouter, create_router_from_config

# Исключения
from src.llm.exceptions import (
    LLMError,
    LLMConnectionError,
    LLMTimeoutError,
    LLMRateLimitError,
    LLMAuthenticationError,
    LLMModelNotFoundError,
    LLMInvalidRequestError,
    LLMUnavailableError,
    LLMFallbackError,
    NoBackendsAvailableError
)

__all__ = [
    # Модели
    "MessageRole",
    "ChatMessage",
    "LLMBackendType",
    "LLMRequest",
    "LLMResponse",
    "StreamChunk",
    "LLMHealthStatus",
    "LLMStats",
    "UsageInfo",

    # Базовый класс
    "LLMBackend",

    # Клиенты
    "VLLMClient",
    "OllamaClient",
    "OpenAIClient",
    "create_openai_client",
    "create_azure_client",
    "create_dashscope_client",

    # Embeddings
    "EmbeddingClient",
    "EmbeddingResponse",
    "cosine_similarity",
    "normalize_vector",

    # Роутер
    "LLMRouter",
    "create_router_from_config",

    # Исключения
    "LLMError",
    "LLMConnectionError",
    "LLMTimeoutError",
    "LLMRateLimitError",
    "LLMAuthenticationError",
    "LLMModelNotFoundError",
    "LLMInvalidRequestError",
    "LLMUnavailableError",
    "LLMFallbackError",
    "NoBackendsAvailableError"
]
