"""
Тесты для LLM модуля KAG
"""

import pytest
import asyncio
from datetime import datetime
from unittest.mock import Mock, patch, AsyncMock, MagicMock

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
from src.llm.base import LLMBackend


# ===========================================
# Тесты моделей
# ===========================================

class TestLLMModels:
    """Тесты Pydantic моделей"""

    def test_chat_message_valid(self):
        """Тест валидного сообщения"""
        msg = ChatMessage(
            role=MessageRole.USER,
            content="Привет, как дела?"
        )
        
        assert msg.role == MessageRole.USER
        assert msg.content == "Привет, как дела?"

    def test_chat_message_empty_content(self):
        """Тест пустого содержимого"""
        with pytest.raises(ValueError):
            ChatMessage(role=MessageRole.USER, content="")

    def test_chat_message_whitespace_content(self):
        """Тест содержимого только из пробелов"""
        with pytest.raises(ValueError):
            ChatMessage(role=MessageRole.USER, content="   ")

    def test_llm_request_valid(self):
        """Тест валидного запроса"""
        request = LLMRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Тест")]
        )
        
        assert request.temperature == 0.7
        assert request.max_tokens == 4096
        assert request.stream is False

    def test_llm_request_empty_messages(self):
        """Тест пустых сообщений"""
        with pytest.raises(ValueError):
            LLMRequest(messages=[])

    def test_llm_request_last_message_not_user(self):
        """Тест что последнее сообщение не от пользователя"""
        with pytest.raises(ValueError):
            LLMRequest(
                messages=[
                    ChatMessage(role=MessageRole.USER, content="Привет"),
                    ChatMessage(role=MessageRole.ASSISTANT, content="Привет!")
                ]
            )

    def test_llm_request_temperature_bounds(self):
        """Тест границ температуры"""
        # Валидные значения
        LLMRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Тест")],
            temperature=0.0
        )
        LLMRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Тест")],
            temperature=2.0
        )
        
        # Невалидные значения
        with pytest.raises(ValueError):
            LLMRequest(
                messages=[ChatMessage(role=MessageRole.USER, content="Тест")],
                temperature=-0.1
            )
        
        with pytest.raises(ValueError):
            LLMRequest(
                messages=[ChatMessage(role=MessageRole.USER, content="Тест")],
                temperature=2.1
            )

    def test_llm_response_generated_text(self):
        """Тест получения сгенерированного текста"""
        response = LLMResponse(
            id="test-id",
            model="test-model",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Тестовый ответ"},
                    "finish_reason": "stop"
                }
            ],
            backend=LLMBackendType.VLLM
        )
        
        assert response.generated_text == "Тестовый ответ"
        assert response.finish_reason == "stop"

    def test_usage_info_calculate(self):
        """Тест расчета токенов"""
        usage = UsageInfo(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=0
        )
        usage.calculate_total()
        
        assert usage.total_tokens == 150

    def test_llm_stats_success_rate(self):
        """Тест процента успешных запросов"""
        stats = LLMStats(
            backend=LLMBackendType.VLLM,
            total_requests=100,
            successful_requests=95,
            failed_requests=5
        )
        
        assert stats.success_rate == 95.0


# ===========================================
# Тесты абстрактного бэкенда
# ===========================================

class TestLLMBackend:
    """Тесты абстрактного базового класса"""

    def test_backend_initialization(self):
        """Тест инициализации"""
        class TestBackend(LLMBackend):
            async def generate(self, request): pass
            async def generate_stream(self, request): pass
            async def health_check(self): pass
            async def get_available_models(self): pass

        backend = TestBackend(
            backend_type=LLMBackendType.VLLM,
            base_url="http://test:8000",
            model="test-model"
        )
        
        assert backend.backend_type == LLMBackendType.VLLM
        assert backend.base_url == "http://test:8000"
        assert backend.model == "test-model"
        assert backend.max_retries == 3


# ===========================================
# Тесты vLLM клиента
# ===========================================

class TestVLLMClient:
    """Тесты vLLM клиента"""

    def test_vllm_initialization(self):
        """Тест инициализации"""
        from src.llm.vllm_client import VLLMClient
        
        client = VLLMClient(
            base_url="http://vllm:8000",
            model="mistral:7b"
        )
        
        assert client.backend_type == LLMBackendType.VLLM
        assert client.model == "mistral:7b"
        assert client.base_url == "http://vllm:8000"

    def test_build_chat_payload(self):
        """Тест построения payload"""
        from src.llm.vllm_client import VLLMClient
        
        client = VLLMClient(
            base_url="http://vllm:8000",
            model="mistral:7b"
        )
        
        request = LLMRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Тест")],
            temperature=0.5,
            max_tokens=1024
        )
        
        payload = client._build_chat_payload(request)
        
        assert payload["model"] == "mistral:7b"
        assert payload["temperature"] == 0.5
        assert payload["max_tokens"] == 1024
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"] == "Тест"


# ===========================================
# Тесты Ollama клиента
# ===========================================

class TestOllamaClient:
    """Тесты Ollama клиента"""

    def test_ollama_initialization(self):
        """Тест инициализации"""
        from src.llm.ollama_client import OllamaClient
        
        client = OllamaClient(
            base_url="http://ollama:11434",
            model="mistral:7b"
        )
        
        assert client.backend_type == LLMBackendType.OLLAMA
        assert client.model == "mistral:7b"

    def test_ollama_payload(self):
        """Тест построения payload"""
        from src.llm.ollama_client import OllamaClient
        
        client = OllamaClient(
            base_url="http://ollama:11434",
            model="mistral:7b"
        )
        
        request = LLMRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Тест")]
        )
        
        payload = client._build_chat_payload(request)
        
        assert payload["model"] == "mistral:7b"
        assert payload["stream"] is False
        assert "options" in payload
        assert "keep_alive" in payload


# ===========================================
# Тесты OpenAI клиента
# ===========================================

class TestOpenAIClient:
    """Тесты OpenAI клиента"""

    def test_openai_initialization(self):
        """Тест инициализации"""
        from src.llm.openai_client import OpenAIClient
        
        client = OpenAIClient(
            base_url="https://api.openai.com",
            model="gpt-4",
            api_key="test-key"
        )
        
        assert client.backend_type == LLMBackendType.OPENAI
        assert client.model == "gpt-4"

    def test_openai_missing_api_key(self):
        """Тест отсутствия API ключа"""
        from src.llm.openai_client import OpenAIClient
        
        with pytest.raises(ValueError, match="API ключ обязателен"):
            OpenAIClient(
                base_url="https://api.openai.com",
                model="gpt-4",
                api_key=""
            )

    def test_create_openai_client_factory(self):
        """Тест фабрики OpenAI"""
        from src.llm.openai_client import create_openai_client
        
        client = create_openai_client(
            api_key="test-key",
            model="gpt-4"
        )
        
        assert client.base_url == "https://api.openai.com"
        assert client.model == "gpt-4"

    def test_create_dashscope_client_factory(self):
        """Тест фабрики DashScope"""
        from src.llm.openai_client import create_dashscope_client
        
        client = create_dashscope_client(
            api_key="test-key",
            model="qwen-max"
        )
        
        assert "dashscope" in client.base_url
        assert client.model == "qwen-max"

    def test_create_azure_client_factory(self):
        """Тест фабрики Azure"""
        from src.llm.openai_client import create_azure_client
        
        client = create_azure_client(
            api_key="test-key",
            base_url="https://test.openai.azure.com",
            model="gpt-4-deployment"
        )
        
        assert "azure.com" in client.base_url
        assert client.model == "gpt-4-deployment"


# ===========================================
# Тесты роутера
# ===========================================

class TestLLMRouter:
    """Тесты роутера"""

    def test_router_initialization(self):
        """Тест инициализации роутера"""
        from src.llm.router import LLMRouter
        
        router = LLMRouter()
        assert len(router._backends) == 0

    def test_add_backend(self):
        """Тест добавления бэкенда"""
        from src.llm.router import LLMRouter
        from src.llm.vllm_client import VLLMClient
        
        router = LLMRouter()
        
        client = VLLMClient(
            base_url="http://vllm:8000",
            model="mistral:7b"
        )
        
        router.add_backend(client, priority=0)
        
        assert LLMBackendType.VLLM in router._backends
        assert len(router._backends) == 1

    def test_remove_backend(self):
        """Тест удаления бэкенда"""
        from src.llm.router import LLMRouter
        from src.llm.vllm_client import VLLMClient
        
        router = LLMRouter()
        client = VLLMClient(
            base_url="http://vllm:8000",
            model="mistral:7b"
        )
        router.add_backend(client)
        
        router.remove_backend(LLMBackendType.VLLM)
        
        assert LLMBackendType.VLLM not in router._backends

    def test_get_sorted_backends(self):
        """Тест сортировки бэкендов"""
        from src.llm.router import LLMRouter
        from src.llm.vllm_client import VLLMClient
        from src.llm.ollama_client import OllamaClient
        
        router = LLMRouter()
        
        vllm = VLLMClient(
            base_url="http://vllm:8000",
            model="mistral:7b"
        )
        router.add_backend(vllm, priority=1)
        
        ollama = OllamaClient(
            base_url="http://ollama:11434",
            model="mistral:7b"
        )
        router.add_backend(ollama, priority=0)
        
        sorted_backends = router._get_sorted_backends()
        
        assert sorted_backends[0].backend.backend_type == LLMBackendType.OLLAMA
        assert sorted_backends[1].backend.backend_type == LLMBackendType.VLLM

    def test_router_stats(self):
        """Тест статистики роутера"""
        from src.llm.router import LLMRouter
        
        router = LLMRouter()
        stats = router.get_stats()
        
        assert "total_backends" in stats
        assert "healthy_backends" in stats
        assert "backends" in stats

    def test_enable_disable_backend(self):
        """Тест включения/отключения бэкенда"""
        from src.llm.router import LLMRouter
        from src.llm.vllm_client import VLLMClient
        
        router = LLMRouter()
        client = VLLMClient(
            base_url="http://vllm:8000",
            model="mistral:7b"
        )
        router.add_backend(client)
        
        # Отключаем
        result = router.disable_backend(LLMBackendType.VLLM)
        assert result is True
        assert router._backends[LLMBackendType.VLLM].enabled is False
        
        # Включаем
        result = router.enable_backend(LLMBackendType.VLLM)
        assert result is True
        assert router._backends[LLMBackendType.VLLM].enabled is True

    def test_enable_nonexistent_backend(self):
        """Тест включения несуществующего бэкенда"""
        from src.llm.router import LLMRouter
        
        router = LLMRouter()
        result = router.enable_backend(LLMBackendType.VLLM)
        
        assert result is False


# ===========================================
# Тесты Circuit Breaker
# ===========================================

class TestCircuitBreaker:
    """Тесты circuit breaker паттерна"""

    def test_backend_entry_record_success(self):
        """Тест записи успеха"""
        from src.llm.router import BackendEntry
        from src.llm.vllm_client import VLLMClient
        
        client = VLLMClient(
            base_url="http://vllm:8000",
            model="mistral:7b"
        )
        entry = BackendEntry(backend=client)
        
        # Записываем несколько ошибок
        entry.record_failure()
        entry.record_failure()
        assert entry.consecutive_failures == 2
        
        # Записываем успех
        entry.record_success()
        assert entry.consecutive_failures == 0
        assert entry.circuit_open is False

    def test_backend_entry_circuit_opens_after_failures(self):
        """Тест открытия circuit после ошибок"""
        from src.llm.router import BackendEntry
        from src.llm.vllm_client import VLLMClient
        
        client = VLLMClient(
            base_url="http://vllm:8000",
            model="mistral:7b"
        )
        entry = BackendEntry(backend=client)
        
        # 3 последовательные ошибки
        entry.record_failure()
        entry.record_failure()
        entry.record_failure()
        
        assert entry.circuit_open is True
        assert entry.consecutive_failures == 3

    def test_backend_entry_should_attempt(self):
        """Тест проверки стоит ли пытаться"""
        from src.llm.router import BackendEntry
        from src.llm.vllm_client import VLLMClient
        
        client = VLLMClient(
            base_url="http://vllm:8000",
            model="mistral:7b"
        )
        entry = BackendEntry(backend=client)
        
        # Healthy бэкенд
        assert entry.should_attempt() is True
        
        # Отключаем
        entry.enabled = False
        assert entry.should_attempt() is False


# ===========================================
# Тесты конфигурации
# ===========================================

class TestConfigIntegration:
    """Тесты интеграции с конфигурацией"""

    def test_settings_get_llm_router_config(self):
        """Тест получения конфигурации роутера"""
        from src.config import Settings
        
        settings = Settings(
            LLM_VLLM_ENABLED=True,
            VLLM_BASE_URL="http://vllm:8000",
            VLLM_MODEL_NAME="mistral:7b",
            LLM_VLLM_PRIORITY=0,
            LLM_OLLAMA_ENABLED=False,
            LLM_OPENAI_ENABLED=False
        )
        
        config = settings.get_llm_router_config()
        
        assert "backends" in config
        assert len(config["backends"]) == 1
        assert config["backends"][0]["type"] == "vllm"
        assert config["backends"][0]["base_url"] == "http://vllm:8000"

    def test_settings_multiple_backends(self):
        """Тест конфигурации с несколькими бэкендами"""
        from src.config import Settings
        
        settings = Settings(
            LLM_VLLM_ENABLED=True,
            VLLM_BASE_URL="http://vllm:8000",
            VLLM_MODEL_NAME="mistral:7b",
            LLM_VLLM_PRIORITY=0,
            LLM_OLLAMA_ENABLED=True,
            OLLAMA_BASE_URL="http://ollama:11434",
            OLLAMA_MODEL="llama2:7b",
            LLM_OLLAMA_PRIORITY=1,
            LLM_OPENAI_ENABLED=False
        )
        
        config = settings.get_llm_router_config()
        
        assert len(config["backends"]) == 2
        assert config["health_check_interval"] == 60
        assert config["auto_recovery"] is True
