"""
Конфигурация приложения
"""

from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Dict, Any


class Settings(BaseSettings):
    """Настройки приложения"""

    APP_VERSION: str = "0.3.0"

    # FastAPI
    FASTAPI_HOST: str = "0.0.0.0"
    FASTAPI_PORT: int = 8000
    FASTAPI_DEBUG: bool = False
    FASTAPI_WORKERS: int = 4

    # Qdrant
    QDRANT_HOST: str = "kag-qdrant"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "kag_documents"

    # Redis
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""

    # Celery
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    # Keycloak
    KEYCLOAK_URL: str = "http://keycloak:8080"
    KEYCLOAK_REALM: str = "kag"
    KEYCLOAK_CLIENT_ID: str = "kag-api"
    KEYCLOAK_CLIENT_SECRET: str = "change_me"

    # Keycloak DB (PostgreSQL)
    KC_DB_USERNAME: str = "keycloak"
    KC_DB_PASSWORD: str = "keycloak_password"
    KC_DB_HOST: str = "keycloak-db"
    KC_DB_PORT: int = 5432
    KC_DB_NAME: str = "keycloak"

    # KAG собственная БД (не Keycloak!)
    KAG_DB_URL: str = ""  # Если задана — config_store использует её приоритетно

    # Casbin
    CASBIN_MODEL_PATH: str = "/app/src/auth/rbac_model.conf"
    CASBIN_POLICY_FILE: str = "/app/src/auth/rbac_policy.csv"

    # OpenTelemetry
    OTEL_SERVICE_NAME: str = "kag-api"
    OTEL_EXPORTER_PROMETHEUS_PORT: int = 9090

    # TLS/SSL (опционально)
    TLS_CERT_PATH: str = ""
    TLS_KEY_PATH: str = ""

    # ===========================================
    # LLM конфигурация
    # ===========================================

    # Основной бэкенд
    LLM_BACKEND: str = "vllm"  # vllm | ollama | openai | auto

    # vLLM настройки
    VLLM_BASE_URL: str = "http://vllm:8000"
    VLLM_MODEL_NAME: str = "mistralai/Mistral-7B-Instruct-v0.2"
    VLLM_API_KEY: str = ""
    VLLM_TIMEOUT: float = 120.0

    # Ollama настройки
    OLLAMA_BASE_URL: str = "http://192.168.50.41:11434"
    OLLAMA_MODEL: str = "phi4-mini:latest"  # Легкая модель (3.8B)
    OLLAMA_TIMEOUT: float = 180.0  # 3 минуты - модель может грузиться долго
    OLLAMA_KEEP_ALIVE: str = "24h"  # 24 часа - не выгружать из памяти

    # OpenAI API настройки (fallback)
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com"
    OPENAI_MODEL: str = "gpt-4"
    OPENAI_TIMEOUT: float = 120.0

    # Azure OpenAI (опционально)
    AZURE_API_KEY: str = ""
    AZURE_BASE_URL: str = ""
    AZURE_MODEL: str = ""
    AZURE_API_VERSION: str = "2024-02-15-preview"

    # DashScope/Qwen API (опционально)
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_MODEL: str = "qwen-max"

    # Embedding модели (Ollama)
    EMBEDDING_BASE_URL: str = "http://192.168.50.41:11434"
    EMBEDDING_MODEL: str = "nomic-embed-text:latest"
    EMBEDDING_TIMEOUT: float = 60.0
    EMBEDDING_DIMENSIONS: int = 768

    # Настройки чанкинга для документов
    # Оптимально для русского языка: 512 токенов ≈ 2000-2500 символов
    # Перекрытие 15% для сохранения контекста между чанками
    CHUNK_SIZE: int = 512  # Размер чанка в токенах (не символах!)
    CHUNK_OVERLAP: int = 77  # 15% перекрытие (512 * 0.15 ≈ 77)

    # Общие настройки LLM
    LLM_MODEL_NAME: str = "mistralai/Mistral-7B-Instruct-v0.2"
    LLM_MAX_TOKENS: int = 4096
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_RETRIES: int = 3
    LLM_RETRY_DELAY: float = 1.0
    LLM_HEALTH_CHECK_INTERVAL: int = 60
    LLM_AUTO_RECOVERY: bool = True

    # Приоритеты бэкендов (0 = наивысший)
    LLM_VLLM_PRIORITY: int = 0
    LLM_OLLAMA_PRIORITY: int = 1
    LLM_OPENAI_PRIORITY: int = 2

    # Включение/отключение бэкендов
    LLM_VLLM_ENABLED: bool = True
    LLM_OLLAMA_ENABLED: bool = True
    LLM_OPENAI_ENABLED: bool = False

    # Логи
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    # Безопасность
    AUTH_ENABLED: bool = False
    KAG_API_TOKEN: str = ""
    CORS_ORIGINS: str = "*"

    # JWT
    JWT_SECRET: str = "kag-system-secret-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # Database (for user auth)
    DATABASE_URL: str = "postgresql://kag:kagpass123@kag-db:5432/kag"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

    def get_llm_router_config(self) -> Dict[str, Any]:
        """
        Получить конфигурацию для LLM роутера из настроек.

        Returns:
            Словарь конфигурации для create_router_from_config()
        """
        backends = []

        # vLLM бэкенд
        if self.LLM_VLLM_ENABLED and self.VLLM_BASE_URL:
            backends.append(
                {
                    "type": "vllm",
                    "base_url": self.VLLM_BASE_URL,
                    "model": self.VLLM_MODEL_NAME or self.LLM_MODEL_NAME,
                    "api_key": self.VLLM_API_KEY or None,
                    "timeout": self.VLLM_TIMEOUT,
                    "priority": self.LLM_VLLM_PRIORITY,
                    "enabled": True,
                }
            )

        # Ollama бэкенд
        if self.LLM_OLLAMA_ENABLED and self.OLLAMA_BASE_URL:
            backends.append(
                {
                    "type": "ollama",
                    "base_url": self.OLLAMA_BASE_URL,
                    "model": self.OLLAMA_MODEL,
                    "timeout": self.OLLAMA_TIMEOUT,
                    "keep_alive": self.OLLAMA_KEEP_ALIVE,
                    "priority": self.LLM_OLLAMA_PRIORITY,
                    "enabled": True,
                }
            )

        # OpenAI бэкенд
        if self.LLM_OPENAI_ENABLED and self.OPENAI_API_KEY:
            backends.append(
                {
                    "type": "openai",
                    "base_url": self.OPENAI_BASE_URL,
                    "model": self.OPENAI_MODEL,
                    "api_key": self.OPENAI_API_KEY,
                    "timeout": self.OPENAI_TIMEOUT,
                    "priority": self.LLM_OPENAI_PRIORITY,
                    "enabled": True,
                }
            )

        return {
            "backends": backends,
            "health_check_interval": self.LLM_HEALTH_CHECK_INTERVAL,
            "auto_recovery": self.LLM_AUTO_RECOVERY,
        }


@lru_cache()
def get_settings() -> Settings:
    """Получить кэшированные настройки"""
    s = Settings()
    # Override from environment if set (for Docker), otherwise use defaults
    # Only use localhost if explicitly set or if qdrant (legacy)
    if s.QDRANT_HOST == "qdrant" or s.QDRANT_HOST == "localhost":
        s.QDRANT_HOST = "kag-qdrant"
    return s
