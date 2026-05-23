"""
Provider Service — единое управление LLM провайдерами и привязкой к функциям.

Архитектура:
  Provider (сущность)  →  хранится в config_store как "providers/{id}"
  FunctionMap (связь)  →  хранится в config_store как "function_map/{name}"

Провайдер — это источник LLM (Ollama, OpenAI, DeepSeek, OpenRouter).
Функция — это роль, которую LLM выполняет (chat, embedding, graph, doc_analysis).

Один провайдер может обслуживать несколько функций.
Одна функция использует ровно один провайдер + модель.

Хранение в config_store:
  providers/{provider_id} = {
    "id": "ollama-main",
    "name": "Локальная Ollama",
    "type": "ollama",
    "url": "http://192.168.50.41:11434",
    "api_key": "",              # всегда пустой при GET, хранится зашифрованным
    "has_api_key": false,
    "models": [],               # список доступных моделей (кэш, обновляется по запросу)
  }

  function_map/{function_name} = {
    "function": "chat",
    "provider_id": "ollama-main",
    "model": "phi4-mini:latest",
    "system_prompt": "...",
    "parameters": { "temperature": 0.7, "max_tokens": 4096 }
  }
"""

from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from loguru import logger

# Типы провайдеров
PROVIDER_TYPES = {
    "ollama":     {"label": "Ollama (локальный)",      "needs_key": False, "url_placeholder": "http://192.168.50.41:11434"},
    "openai":     {"label": "OpenAI API",              "needs_key": True,  "url_placeholder": "https://api.openai.com"},
    "deepseek":   {"label": "DeepSeek API",            "needs_key": True,  "url_placeholder": "https://api.deepseek.com"},
    "openrouter": {"label": "OpenRouter API",          "needs_key": True,  "url_placeholder": "https://openrouter.ai/api"},
    "custom":     {"label": "Custom OpenAI-совместимый","needs_key": True,  "url_placeholder": "https://your-server.com"},
}

# Список функций, которые могут использовать LLM
FUNCTION_DEFINITIONS = {
    "chat": {
        "label": "Чат с документами",
        "icon": "💬",
        "description": "Основная LLM для ответов пользователю в чате",
        "supports_prompt": True,
        "supports_parameters": True,
    },
    "embedding": {
        "label": "Embedding (векторизация)",
        "icon": "📐",
        "description": "Модель для превращения текста в векторы",
        "supports_prompt": False,
        "supports_parameters": False,
    },
    "graph": {
        "label": "Извлечение графа знаний",
        "icon": "🕸️",
        "description": "LLM для извлечения сущностей и связей из документов",
        "supports_prompt": True,
        "supports_parameters": True,
    },
    "doc_analysis": {
        "label": "Анализ документов",
        "icon": "📄",
        "description": "LLM для классификации и анализа содержимого документов",
        "supports_prompt": True,
        "supports_parameters": True,
    },
}


@dataclass
class ProviderConfig:
    """Конфигурация одного LLM провайдера"""
    id: str                          # Уникальный ID (например "ollama-main")
    name: str                        # Человеческое название (например "Локальная Ollama")
    type: str                        # Тип: ollama, openai, deepseek, openrouter, custom
    url: str                         # URL API
    api_key: str = ""                # API ключ (передаётся только при записи)
    has_api_key: bool = False        # Флаг: есть ли ключ (для UI)
    enabled: bool = True             # Включён ли провайдер
    models: List[str] = field(default_factory=list)  # Кэш списка моделей
    settings: Dict[str, Any] = field(default_factory=dict)  # Дополнительные настройки

    def to_dict(self, include_secret: bool = False) -> dict:
        """Сериализация для API (без ключа по умолчанию)"""
        d = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "url": self.url,
            "enabled": self.enabled,
            "models": self.models,
            "has_api_key": bool(self.api_key) or self.has_api_key,
            "settings": self.settings,
        }
        if include_secret:
            d["api_key"] = self.api_key
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            type=data.get("type", "ollama"),
            url=data.get("url", ""),
            api_key=data.get("api_key", ""),
            has_api_key=data.get("has_api_key", False) or bool(data.get("api_key")),
            enabled=data.get("enabled", True),
            models=data.get("models", []),
            settings=data.get("settings", {}),
        )


@dataclass
class FunctionMap:
    """Привязка функции к провайдеру и модели"""
    function: str                    # Название функции (chat, embedding, graph, doc_analysis)
    provider_id: str                 # ID провайдера
    model: str = ""                  # Модель (если пусто — будет использована модель провайдера по умолчанию)
    system_prompt: str = ""          # Системный промпт (только для функций, где supports_prompt=True)
    parameters: Dict[str, Any] = field(default_factory=lambda: {
        "temperature": 0.7,
        "max_tokens": 4096,
    })

    def to_dict(self) -> dict:
        return {
            "function": self.function,
            "provider_id": self.provider_id,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "parameters": self.parameters,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FunctionMap":
        return cls(
            function=data.get("function", ""),
            provider_id=data.get("provider_id", ""),
            model=data.get("model", ""),
            system_prompt=data.get("system_prompt", ""),
            parameters=data.get("parameters", {"temperature": 0.7, "max_tokens": 4096}),
        )


class ProviderService:
    """
    Сервис управления провайдерами и привязкой к функциям.

    Вся логика работы с config_store (PostgreSQL) сосредоточена здесь.
    """

    def __init__(self):
        self._config_store = None  # Lazy init
        self._provider_cache: Dict[str, ProviderConfig] = {}
        self._function_cache: Dict[str, FunctionMap] = {}
        self._cache_loaded = False

    @property
    def config_store(self):
        if self._config_store is None:
            from src.api.services.config_store import config_store as cs
            self._config_store = cs
        return self._config_store

    def _load_cache(self):
        """Загрузить всё из БД в кэш"""
        if self._cache_loaded:
            return

        # Загружаем провайдеров
        providers_data = self.config_store.get_all("providers") or {}
        self._provider_cache = {}
        for pid, pdata in providers_data.items():
            if isinstance(pdata, dict):
                self._provider_cache[pid] = ProviderConfig.from_dict(pdata)

        # Загружаем функции
        functions_data = self.config_store.get_all("function_map") or {}
        self._function_cache = {}
        for fname, fdata in functions_data.items():
            if isinstance(fdata, dict):
                self._function_cache[fname] = FunctionMap.from_dict(fdata)

        self._cache_loaded = True
        logger.debug(f"ProviderService: загружено {len(self._provider_cache)} провайдеров, {len(self._function_cache)} функций")

    def _invalidate_cache(self):
        """Сбросить кэш при изменении"""
        self._cache_loaded = False
        self._provider_cache = {}
        self._function_cache = {}

    # ===========================================
    # Провайдеры
    # ===========================================

    def list_providers(self) -> List[dict]:
        """Получить список всех провайдеров (без API-ключей)"""
        self._load_cache()
        return [p.to_dict(include_secret=False) for p in self._provider_cache.values()]

    def get_provider(self, provider_id: str) -> Optional[dict]:
        """Получить провайдера по ID (без API-ключа)"""
        self._load_cache()
        p = self._provider_cache.get(provider_id)
        return p.to_dict(include_secret=False) if p else None

    def get_provider_with_key(self, provider_id: str) -> Optional[ProviderConfig]:
        """Получить провайдера с API-ключом (для внутреннего использования)"""
        self._load_cache()
        return self._provider_cache.get(provider_id)

    def save_provider(self, config: ProviderConfig) -> bool:
        """Сохранить/обновить провайдера"""
        try:
            existing = self.config_store.get("providers", config.id) or {}
            if isinstance(existing, str):
                existing = {}

            # Сохраняем в БД (с ключом, если передан)
            save_data = {
                "id": config.id,
                "name": config.name,
                "type": config.type,
                "url": config.url,
                "enabled": config.enabled,
                "models": config.models,
                "settings": config.settings,
            }

            # API-ключ шифруем отдельно или сохраняем
            if config.api_key:
                save_data["has_api_key"] = True
                save_data["api_key"] = config.api_key  # config_store сам шифрует, если надо
            else:
                # Сохраняем старый ключ, если новый не передан
                save_data["has_api_key"] = existing.get("has_api_key", False)
                save_data["api_key"] = existing.get("api_key", "")

            success = self.config_store.set("providers", config.id, save_data)
            if success:
                self._invalidate_cache()
                logger.info(f"Провайдер {config.id} ({config.name}) сохранён")
            return success
        except Exception as e:
            logger.error(f"Ошибка сохранения провайдера {config.id}: {e}")
            return False

    def delete_provider(self, provider_id: str) -> bool:
        """Удалить провайдера и все привязки к нему"""
        try:
            # Удаляем провайдера
            success = self.config_store.delete("providers", provider_id)
            if success:
                # Удаляем все function_map, которые ссылались на этот провайдер
                functions_data = self.config_store.get_all("function_map") or {}
                for fname, fdata in functions_data.items():
                    if isinstance(fdata, dict) and fdata.get("provider_id") == provider_id:
                        self.config_store.delete("function_map", fname)
                        logger.info(f"Удалена привязка функции {fname} (провайдер {provider_id} удалён)")

                self._invalidate_cache()
                logger.info(f"Провайдер {provider_id} удалён")
            return success
        except Exception as e:
            logger.error(f"Ошибка удаления провайдера {provider_id}: {e}")
            return False

    async def fetch_provider_models(self, provider_id: str) -> List[str]:
        """Получить список моделей провайдера через его API"""
        self._load_cache()
        provider = self._provider_cache.get(provider_id)
        if not provider:
            return []

        import httpx
        try:
            if provider.type == "ollama":
                url = f"{provider.url}/api/tags"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        models = [m.get("name", "") for m in data.get("models", [])]
                    else:
                        models = []
            else:
                # OpenAI-совместимый: /v1/models
                url = f"{provider.url.rstrip('/')}/v1/models"
                headers = {}
                if provider.api_key:
                    headers["Authorization"] = f"Bearer {provider.api_key}"
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        models = [m.get("id", "") for m in data.get("data", [])]
                    else:
                        models = []

            # Обновляем кэш в БД
            if models:
                save_data = {
                    "id": provider.id,
                    "name": provider.name,
                    "type": provider.type,
                    "url": provider.url,
                    "api_key": provider.api_key,
                    "has_api_key": bool(provider.api_key),
                    "enabled": provider.enabled,
                    "models": models,
                    "settings": provider.settings,
                }
                self.config_store.set("providers", provider_id, save_data)
                self._invalidate_cache()

            return models
        except Exception as e:
            logger.error(f"Ошибка получения моделей провайдера {provider_id}: {e}")
            return []

    # ===========================================
    # Привязка функций к провайдерам
    # ===========================================

    def list_function_maps(self) -> List[dict]:
        """Получить все привязки функций"""
        self._load_cache()
        return [f.to_dict() for f in self._function_cache.values()]

    def get_function_map(self, function_name: str) -> Optional[dict]:
        """Получить привязку функции"""
        self._load_cache()
        fm = self._function_cache.get(function_name)
        return fm.to_dict() if fm else None

    def save_function_map(self, fm: FunctionMap) -> bool:
        """Сохранить привязку функции"""
        try:
            # Валидация: провайдер должен существовать
            provider = self._provider_cache.get(fm.provider_id)
            if not provider:
                logger.warning(f"Провайдер {fm.provider_id} не найден при сохранении функции {fm.function}")
                return False

            success = self.config_store.set("function_map", fm.function, fm.to_dict())
            if success:
                self._invalidate_cache()
                logger.info(f"Привязка функции {fm.function} → {fm.provider_id}/{fm.model} сохранена")
            return success
        except Exception as e:
            logger.error(f"Ошибка сохранения привязки функции {fm.function}: {e}")
            return False

    def get_function_provider(self, function_name: str) -> Optional[tuple[ProviderConfig, FunctionMap]]:
        """
        Получить провайдера и привязку для функции.
        Возвращает (ProviderConfig, FunctionMap) или None.
        """
        self._load_cache()
        fm = self._function_cache.get(function_name)
        if not fm:
            # Пробуем дефолт: берём первого включённого провайдера
            for p in self._provider_cache.values():
                if p.enabled:
                    fm = FunctionMap(
                        function=function_name,
                        provider_id=p.id,
                        model=p.models[0] if p.models else "phi4-mini:latest",
                    )
                    break
            if not fm:
                return None

        provider = self._provider_cache.get(fm.provider_id)
        if not provider or not provider.enabled:
            return None

        return (provider, fm)

    # ===========================================
    # Вспомогательные методы
    # ===========================================

    def get_default_provider_id(self) -> Optional[str]:
        """Получить ID первого включённого провайдера"""
        self._load_cache()
        for p in self._provider_cache.values():
            if p.enabled:
                return p.id
        return None

    def ensure_defaults(self) -> bool:
        """
        Создать провайдера по умолчанию и дефолтные привязки,
        если в БД ещё ничего нет.
        """
        self._load_cache()
        if self._provider_cache:
            return True  # Уже есть провайдеры — не трогаем

        # Создаём провайдера по умолчанию (Ollama)
        default_provider = ProviderConfig(
            id="ollama-main",
            name="Локальная Ollama",
            type="ollama",
            url="http://192.168.50.41:11434",
            enabled=True,
        )

        if not self.save_provider(default_provider):
            return False

        # Создаём дефолтные привязки для всех функций
        default_maps = {
            "chat":         FunctionMap("chat", "ollama-main", "phi4-mini:latest", system_prompt=""),
            "embedding":    FunctionMap("embedding", "ollama-main", "nomic-embed-text"),
            "graph":        FunctionMap("graph", "ollama-main", "phi4-mini:latest", system_prompt="Извлеки сущности и связи из текста"),
            "doc_analysis": FunctionMap("doc_analysis", "ollama-main", "phi4-mini:latest"),
        }

        for fm in default_maps.values():
            self.save_function_map(fm)

        logger.info("Созданы дефолтные провайдер и привязки функций")
        return True

    def create_llm_client(self, provider_config: ProviderConfig, function_map: FunctionMap):
        """
        Создать LLM клиент для указанного провайдера и функции.
        Возвращает кортеж (backend_type, url, api_key, model).
        Используется существующими сервисами (model_manager, entity_extractor и т.д.).
        """
        return (
            provider_config.type,  # backend_type (ollama, openai, deepseek, openrouter)
            provider_config.url,   # url
            provider_config.api_key,  # api_key
            function_map.model,    # model
            function_map.system_prompt,  # system_prompt
            function_map.parameters,  # parameters
        )

    def create_ollama_client(self, provider_config: ProviderConfig, model: str):
        """
        Создать экземпляр OllamaClient для использования в существующем LLMRouter.
        """
        from src.llm.ollama_client import OllamaClient
        return OllamaClient(
            base_url=provider_config.url,
            model=model,
            timeout=120.0,
            max_retries=3,
            retry_delay=1.0,
            keep_alive="24h",
        )

    def create_openai_client(self, provider_config: ProviderConfig, model: str):
        """
        Создать экземпляр OpenAIClient для OpenAI-совместимых провайдеров.
        """
        from src.llm.openai_client import OpenAIClient
        return OpenAIClient(
            base_url=provider_config.url,
            model=model,
            api_key=provider_config.api_key,
            timeout=120.0,
            max_retries=3,
            retry_delay=1.0,
        )


# Singleton
provider_service = ProviderService()
