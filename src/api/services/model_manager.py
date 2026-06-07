"""
Менеджер моделей LLM и Embeddings

Отвечает за:
- Управление подключенными моделями
- Переключение между бэкендами (Ollama/vLLM)
- Получение списка доступных моделей
- Выбор активной модели для генерации
- Выбор embedding модели
- Конфигурация через админ-панель
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
import json
from pathlib import Path
from loguru import logger
from pydantic import BaseModel, Field

from src.llm import (
    LLMRouter,
    LLMBackendType,
    VLLMClient,
    OllamaClient,
    EmbeddingClient
)
from src.api.services.config_store import config_store
from src.config import get_settings


class ModelInfo(BaseModel):
    """Информация о модели"""
    name: str = Field(..., description="Название модели")
    backend: LLMBackendType = Field(..., description="Бэкенд")
    model_type: str = Field(..., description="Тип: llm | embedding")
    status: str = Field(default="unknown", description="status: available | loaded | error")
    parameters: Optional[str] = Field(default=None, description="Параметры (размер)")
    digest: Optional[str] = Field(default=None, description="Digest хэш")
    modified_at: Optional[str] = Field(default=None, description="Время изменения")
    size: Optional[int] = Field(default=None, description="Размер в байтах")
    is_active: bool = Field(default=False, description="Активна ли сейчас")


class BackendConfig(BaseModel):
    """Конфигурация бэкенда"""
    backend_type: LLMBackendType = Field(..., description="Тип бэкенда")
    base_url: str = Field(..., description="URL бэкенда")
    enabled: bool = Field(default=True, description="Включен ли")
    priority: int = Field(default=0, description="Приоритет")
    active_model: Optional[str] = Field(default=None, description="Активная модель")
    embedding_model: Optional[str] = Field(default=None, description="Embedding модель")


class ModelManagerState(BaseModel):
    """Состояние менеджера моделей"""
    active_llm_backend: Optional[LLMBackendType] = Field(default=None)
    active_llm_model: Optional[str] = Field(default=None)
    active_embedding_model: Optional[str] = Field(default=None)
    embedding_dimensions: int = Field(default=768)
    backends: Dict[str, BackendConfig] = Field(default_factory=dict)
    last_updated: datetime = Field(default_factory=datetime.utcnow)


class ModelManager:
    """
    Менеджер моделей для KAG.

    Управляет подключением к Ollama/vLLM, переключением моделей
    и конфигурацией embedding.
    """

    def __init__(self, state_file: Optional[Path] = None):
        """
        Инициализация менеджера.

        Args:
            state_file: Файл для сохранения состояния
        """
        self._state_file = state_file or Path("/app/data/model_manager_state.json")
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            logger.warning(f"Не могу создать директорию для {self._state_file}, использую /tmp")
            self._state_file = Path("/tmp/model_manager_state.json")

        # Компоненты
        self._llm_router: Optional[LLMRouter] = None
        self._embedding_client: Optional[EmbeddingClient] = None
        
        # Текущее состояние
        self._state = ModelManagerState()
        
        # Загружаем сохраненное состояние
        self._load_state()

        logger.info(f"ModelManager инициализирован, state_file={self._state_file}")

    def _load_state(self):
        """Загрузить состояние из файла"""
        if self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text(encoding='utf-8'))
                self._state = ModelManagerState(**data)
                logger.info(f"Состояние загружено: {self._state_file}")
            except Exception as e:
                logger.error(f"Ошибка загрузки состояния: {e}")

    def _save_state(self):
        """Сохранить состояние в файл"""
        try:
            data = self._state.model_dump()
            # Преобразуем datetime в строки
            data['last_updated'] = self._state.last_updated.isoformat()
            
            self._state_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding='utf-8'
            )
            logger.debug(f"Состояние сохранено: {self._state_file}")
        except Exception as e:
            logger.error(f"Ошибка сохранения состояния: {e}")

    async def initialize(self):
        """
        Инициализировать все компоненты из конфигурации.
        
        Приоритет:
        1. Настройки из Setup Wizard (PostgreSQL)
        2. Настройки из .env / config.py
        """
        logger.info("Инициализация ModelManager...")

        # Пробуем загрузить настройки из БД (Setup Wizard)
        db_config = config_store.get("llm", "default")
        
        if db_config:
            logger.info("Загрузка настроек LLM из PostgreSQL (Setup Wizard)")
            await self._init_from_db_config(db_config)
        else:
            logger.info("Загрузка настроек LLM из config.py / .env")
            await self._init_from_env()

    async def _init_from_db_config(self, db_config):
        """Инициализация на основе данных из БД"""
        try:
            llm_type = db_config.get("type", "ollama")
            host = db_config.get("host", "localhost")
            port = db_config.get("port", 11434)
            model = db_config.get("model", "phi4-mini:latest")
            
            base_url = f"http://{host}:{port}"
            
            self._llm_router = LLMRouter(
                health_check_interval=60,
                auto_recovery=True
            )

            if llm_type == "ollama":
                ollama = OllamaClient(
                    base_url=base_url,
                    model=model,
                    timeout=120.0,
                    max_retries=3,
                    retry_delay=1.0,
                    keep_alive="24h"  # Исправлено с "-1" на "24h"
                )
                self._llm_router.add_backend(ollama, priority=0, enabled=True)
                logger.info(f"Добавлен Ollama бэкенд: {model} @ {base_url}")
            elif llm_type == "vllm":
                vllm = VLLMClient(
                    base_url=base_url,
                    model=model,
                    timeout=120.0
                )
                self._llm_router.add_backend(vllm, priority=0, enabled=True)
                logger.info(f"Добавлен vLLM бэкенд: {model} @ {base_url}")

            # Обновляем состояние (State)
            self._state.active_llm_backend = LLMBackendType.OLLAMA if llm_type == "ollama" else LLMBackendType.VLLM
            self._state.active_llm_model = model
            self._state.backends[self._state.active_llm_backend.value] = BackendConfig(
                backend_type=self._state.active_llm_backend,
                base_url=base_url,
                enabled=True,
                priority=0
            )

            # Embedding клиент
            emb_config = config_store.get("embedding", "default")
            if emb_config:
                self._embedding_client = EmbeddingClient(
                    base_url=base_url,  # Обычно на том же сервере
                    model=emb_config.get("model", "qwen3-embedding:4b"),
                    timeout=60.0
                )
                self._state.active_embedding_model = emb_config.get("model", "qwen3-embedding:4b")
                self._state.embedding_dimensions = emb_config.get("dimensions", 4096) # Или из конфига
                logger.info(f"Добавлен Embedding клиент: {emb_config.get('model')}")

            # Запускаем health monitoring
            await self._llm_router.start_health_monitoring()
            
            # Сохраняем состояние
            self._save_state()
                
        except Exception as e:
            logger.error(f"Ошибка инициализации из БД: {e}")
            # Фоллбэк на env
            await self._init_from_env()

    async def _init_from_env(self):
        """Инициализация на основе переменных окружения"""
        from src.config import get_settings
        settings = get_settings()
        
        # Создаем LLM роутер
        llm_config = settings.get_llm_router_config()
        self._llm_router = LLMRouter(
            health_check_interval=settings.LLM_HEALTH_CHECK_INTERVAL,
            auto_recovery=settings.LLM_AUTO_RECOVERY
        )

        # Добавляем бэкенды в роутер
        if settings.LLM_VLLM_ENABLED and settings.VLLM_BASE_URL:
            vllm = VLLMClient(
                base_url=settings.VLLM_BASE_URL,
                model=settings.VLLM_MODEL_NAME or settings.LLM_MODEL_NAME,
                api_key=settings.VLLM_API_KEY or None,
                timeout=settings.VLLM_TIMEOUT,
                max_retries=settings.LLM_MAX_RETRIES,
                retry_delay=settings.LLM_RETRY_DELAY
            )
            self._llm_router.add_backend(
                vllm,
                priority=settings.LLM_VLLM_PRIORITY,
                enabled=settings.LLM_VLLM_ENABLED
            )

        if settings.LLM_OLLAMA_ENABLED and settings.OLLAMA_BASE_URL:
            ollama = OllamaClient(
                base_url=settings.OLLAMA_BASE_URL,
                model=settings.OLLAMA_MODEL,
                timeout=settings.OLLAMA_TIMEOUT,
                max_retries=settings.LLM_MAX_RETRIES,
                retry_delay=settings.LLM_RETRY_DELAY,
                keep_alive=settings.OLLAMA_KEEP_ALIVE
            )
            self._llm_router.add_backend(
                ollama,
                priority=settings.LLM_OLLAMA_PRIORITY,
                enabled=settings.LLM_OLLAMA_ENABLED
            )

        if settings.LLM_OPENAI_ENABLED and settings.OPENAI_API_KEY:
            from src.llm.openai_client import OpenAIClient
            openai = OpenAIClient(
                base_url=settings.OPENAI_BASE_URL,
                model=settings.OPENAI_MODEL,
                api_key=settings.OPENAI_API_KEY,
                timeout=settings.OPENAI_TIMEOUT,
                max_retries=settings.LLM_MAX_RETRIES,
                retry_delay=settings.LLM_RETRY_DELAY
            )
            self._llm_router.add_backend(
                openai,
                priority=settings.LLM_OPENAI_PRIORITY,
                enabled=settings.LLM_OPENAI_ENABLED
            )

        # Создаем embedding клиент
        self._embedding_client = EmbeddingClient(
            base_url=settings.EMBEDDING_BASE_URL,
            model=settings.EMBEDDING_MODEL,
            timeout=settings.EMBEDDING_TIMEOUT
        )

        # Сохраняем конфигурацию бэкендов
        self._state.backends = {
            LLMBackendType.VLLM.value: BackendConfig(
                backend_type=LLMBackendType.VLLM,
                base_url=settings.VLLM_BASE_URL,
                enabled=settings.LLM_VLLM_ENABLED,
                priority=settings.LLM_VLLM_PRIORITY
            ),
            LLMBackendType.OLLAMA.value: BackendConfig(
                backend_type=LLMBackendType.OLLAMA,
                base_url=settings.OLLAMA_BASE_URL,
                enabled=settings.LLM_OLLAMA_ENABLED,
                priority=settings.LLM_OLLAMA_PRIORITY
            ),
            LLMBackendType.OPENAI.value: BackendConfig(
                backend_type=LLMBackendType.OPENAI,
                base_url=settings.OPENAI_BASE_URL,
                enabled=settings.LLM_OPENAI_ENABLED and bool(settings.OPENAI_API_KEY),
                priority=settings.LLM_OPENAI_PRIORITY
            )
        }

        # Запускаем health monitoring
        await self._llm_router.start_health_monitoring()

        # Проверяем embedding
        emb_health = await self._embedding_client.health_check()
        if emb_health.get("healthy"):
            self._state.active_embedding_model = settings.EMBEDDING_MODEL
            self._state.embedding_dimensions = emb_health.get("dimensions", 768)
            logger.info(f"Embedding модель активна: {settings.EMBEDDING_MODEL}")
        else:
            logger.warning(f"Embedding модель недоступна: {emb_health.get('error')}")

        # Определяем активный бэкенд
        primary = self._llm_router.get_primary_backend()
        if primary:
            self._state.active_llm_backend = primary
            backend_entry = self._llm_router._backends.get(primary)
            if backend_entry:
                self._state.active_llm_model = backend_entry.backend.model

        self._save_state()
        logger.info("ModelManager инициализирован успешно")

    # ===========================================
    # LLM методы
    # ===========================================

    async def list_llm_models(self) -> List[ModelInfo]:
        """
        Получить список всех доступных LLM моделей.

        Returns:
            Список информации о моделях
        """
        models = []

        for backend_type, entry in self._llm_router._backends.items():
            if not entry.enabled:
                continue

            try:
                backend = entry.backend
                available_models = await backend.get_available_models()

                for model_name in available_models:
                    model_info = ModelInfo(
                        name=model_name,
                        backend=backend_type,
                        model_type="llm",
                        status="available",
                        is_active=(
                            backend_type == self._state.active_llm_backend and
                            model_name == self._state.active_llm_model
                        )
                    )
                    models.append(model_info)

            except Exception as e:
                logger.error(f"Ошибка получения моделей {backend_type.value}: {e}")
                models.append(ModelInfo(
                    name="error",
                    backend=backend_type,
                    model_type="llm",
                    status="error",
                    parameters=str(e)
                ))

        return models

    async def list_embedding_models(self) -> List[ModelInfo]:
        """
        Получить список всех моделей Ollama (для выбора embedding).

        Returns:
            Список всех доступных моделей
        """
        if not self._embedding_client:
            return []

        try:
            from src.llm.ollama_client import OllamaClient
            ollama = OllamaClient(base_url=self._embedding_client.base_url)
            
            models_response = await ollama.get_available_models()
            
            # Возвращаем ВСЕ модели - пользователь сам выберет
            embedding_models = []
            for model_name in models_response:
                embedding_models.append(ModelInfo(
                    name=model_name,
                    backend=LLMBackendType.OLLAMA,
                    model_type="embedding",
                    status="available",
                    is_active=(model_name == self._state.active_embedding_model)
                ))

            await ollama.close()
            return embedding_models

        except Exception as e:
            logger.error(f"Ошибка получения embedding моделей: {e}")
            return []

    async def get_ollama_models_detailed(self) -> List[Dict[str, Any]]:
        """
        Получить детальную информацию о моделях Ollama.
        Использует прямой HTTP запрос для надежности.
        """
        try:
            import os
            import httpx
            
            base_url = os.getenv("OLLAMA_BASE_URL", "http://192.168.50.41:11434")
            if not base_url or "host.docker.internal" in base_url:
                base_url = "http://192.168.50.41:11434"
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{base_url}/api/tags")
                
                if response.status_code == 200:
                    data = response.json()
                    return data.get("models", [])
                
                logger.error(f"Ollama вернул статус {response.status_code}: {response.text}")
                return []
                
        except Exception as e:
            logger.error(f"Ошибка получения детальной информации об Ollama моделях: {e}")
            return []

    async def switch_llm_model(
        self,
        backend_type: LLMBackendType,
        model_name: str
    ) -> bool:
        """
        Переключить активную LLM модель.

        Args:
            backend_type: Тип бэкенда
            model_name: Название модели

        Returns:
            True если успешно
        """
        entry = self._llm_router._backends.get(backend_type)
        if not entry:
            logger.error(f"Бэкенд не найден: {backend_type.value}")
            return False

        # Обновляем модель в бэкенде
        entry.backend.model = model_name
        
        # Обновляем состояние
        self._state.active_llm_backend = backend_type
        self._state.active_llm_model = model_name
        self._state.last_updated = datetime.utcnow()
        
        self._save_state()
        
        logger.info(
            f"Модель переключена: {backend_type.value}/{model_name}"
        )
        return True

    async def switch_embedding_model(self, model_name: str) -> bool:
        """
        Переключить embedding модель.

        Args:
            model_name: Название embedding модели

        Returns:
            True если успешно
        """
        if not self._embedding_client:
            logger.error("Embedding клиент не инициализирован")
            return False

        # Проверяем что модель доступна
        from src.llm.ollama_client import OllamaClient
        ollama = OllamaClient(base_url=self._embedding_client.base_url)
        
        try:
            models = await ollama.get_available_models()
            if model_name not in models:
                logger.error(f"Модель не найдена: {model_name}")
                await ollama.close()
                return False
        finally:
            await ollama.close()

        # Переключаем
        self._embedding_client.model = model_name
        self._state.active_embedding_model = model_name
        self._state.last_updated = datetime.utcnow()
        
        self._save_state()
        
        logger.info(f"Embedding модель переключена: {model_name}")
        return True

    async def pull_model(self, model_name: str) -> Dict[str, Any]:
        """
        Загрузить модель из Ollama registry.

        Args:
            model_name: Название модели

        Returns:
            Результат загрузки
        """
        if not self._embedding_client:
            return {"error": "Embedding клиент не инициализирован"}

        try:
            from src.llm.ollama_client import OllamaClient
            ollama = OllamaClient(base_url=self._embedding_client.base_url)
            
            await ollama.pull_model(model_name)
            
            # Проверяем что модель появилась в списке
            models = await ollama.get_available_models()
            
            await ollama.close()
            
            if model_name in models:
                return {
                    "status": "success",
                    "model": model_name,
                    "message": f"Модель загружена: {model_name}"
                }
            else:
                return {
                    "status": "loading",
                    "model": model_name,
                    "message": f"Модель загружается: {model_name}"
                }

        except Exception as e:
            return {
                "status": "error",
                "model": model_name,
                "error": str(e)
            }

    async def delete_model(self, model_name: str) -> bool:
        """
        Удалить модель из Ollama.

        Args:
            model_name: Название модели

        Returns:
            True если успешно
        """
        if not self._embedding_client:
            return False

        try:
            from src.llm.ollama_client import OllamaClient
            ollama = OllamaClient(base_url=self._embedding_client.base_url)
            client = await ollama._get_client()
            
            response = await client.delete("/api/delete", json={"name": model_name})
            
            await ollama.close()
            
            if response.status_code == 200:
                logger.info(f"Модель удалена: {model_name}")
                return True
            else:
                logger.error(f"Ошибка удаления модели: {response.text}")
                return False

        except Exception as e:
            logger.error(f"Ошибка удаления модели: {e}")
            return False

    # ===========================================
    # Статус и health checks
    # ===========================================

    async def get_status(self) -> Dict[str, Any]:
        """Получить полный статус системы"""
        llm_health = await self._llm_router.health_check()
        emb_health = await self._embedding_client.health_check() if self._embedding_client else {"healthy": False}

        return {
            "llm_backends": {
                backend_type.value: {
                    "healthy": status.healthy,
                    "model": status.model,
                    "response_time_ms": status.response_time_ms,
                    "error": status.error
                }
                for backend_type, status in llm_health.items()
            },
            "embedding": {
                "healthy": emb_health.get("healthy", False),
                "model": self._state.active_embedding_model,
                "dimensions": self._state.embedding_dimensions,
                "error": emb_health.get("error")
            },
            "active_config": {
                "llm_backend": self._state.active_llm_backend.value if self._state.active_llm_backend else None,
                "llm_model": self._state.active_llm_model,
                "embedding_model": self._state.active_embedding_model
            },
            "stats": self._llm_router.get_stats()
        }

    # ===========================================
    # Геттеры для компонентов
    # ===========================================

    @property
    def llm_router(self) -> Optional[LLMRouter]:
        """Получить LLM роутер"""
        return self._llm_router

    @property
    def embedding_client(self) -> Optional[EmbeddingClient]:
        """Получить embedding клиент"""
        return self._embedding_client

    @property
    def state(self) -> ModelManagerState:
        """Получить текущее состояние"""
        return self._state

    async def close(self):
        """Закрыть все соединения"""
        if self._llm_router:
            self._llm_router.stop_health_monitoring()
            await self._llm_router.__aexit__(None, None, None)
        
        if self._embedding_client:
            await self._embedding_client.close()
        
        logger.info("ModelManager закрыт")


# Глобальный экземпляр
model_manager = ModelManager()
