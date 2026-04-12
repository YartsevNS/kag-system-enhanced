# LLM Модуль - Документация

## Обзор

LLM модуль предоставляет **единый интерфейс** для работы с различными бэкендами языковых моделей с автоматическим fallback и health checks.

## Архитектура

```
┌─────────────────────────────────────────┐
│         KAG Application                 │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│         LLMRouter                       │
│  ┌───────────────────────────────────┐  │
│  │  Circuit Breaker & Fallback       │  │
│  │  Health Checks                    │  │
│  │  Priority Management              │  │
│  └───────────────────────────────────┘  │
└───────┬─────────┬─────────┬─────────────┘
        │         │         │
        ▼         ▼         ▼
   ┌────────┐ ┌────────┐ ┌────────┐
   │  vLLM  │ │ Ollama │ │ OpenAI │
   │ (prod) │ │ (dev)  │ │ (fall) │
   └────────┘ └────────┘ └────────┘
```

## Поддерживаемые бэкенды

| Бэкенд | Назначение | 152-ФЗ | Приоритет |
|--------|-----------|--------|-----------|
| **vLLM** | Production, GPU сервер | ✅ Да | 0 (высший) |
| **Ollama** | Development, локальный | ✅ Да | 1 |
| **OpenAI** | Fallback, testing | ❌ Нет | 2 (низший) |

## Быстрый старт

### 1. Базовое использование

```python
from src.llm import (
    LLMRouter, 
    VLLMClient, 
    LLMRequest, 
    ChatMessage, 
    MessageRole
)

# Создаем роутер
router = LLMRouter()

# Добавляем vLLM бэкенд
vllm = VLLMClient(
    base_url="http://vllm:8000",
    model="mistralai/Mistral-7B-Instruct-v0.2"
)
router.add_backend(vllm, priority=0)

# Генерация ответа
request = LLMRequest(
    messages=[
        ChatMessage(role=MessageRole.USER, content="Что такое KAG?")
    ],
    temperature=0.7,
    max_tokens=1024
)

response = await router.generate(request)
print(response.generated_text)
```

### 2. Из конфигурации (.env)

```python
from src.config import get_settings
from src.llm import create_router_from_config

settings = get_settings()
config = settings.get_llm_router_config()

router = create_router_from_config(config)
```

### 3. Несколько бэкендов с fallback

```python
from src.llm import (
    LLMRouter,
    VLLMClient,
    OllamaClient,
    OpenAIClient
)

router = LLMRouter()

# Production: vLLM (приоритет 0)
router.add_backend(
    VLLMClient(
        base_url="http://vllm:8000",
        model="mistral:7b"
    ),
    priority=0
)

# Development: Ollama (приоритет 1)
router.add_backend(
    OllamaClient(
        base_url="http://ollama:11434",
        model="mistral:7b"
    ),
    priority=1
)

# Fallback: OpenAI (приоритет 2)
router.add_backend(
    OpenAIClient(
        base_url="https://api.openai.com",
        model="gpt-4",
        api_key="sk-..."
    ),
    priority=2
)

# Автоматический health check
await router.start_health_monitoring()
```

## Конфигурация (.env)

### Production (152-ФЗ compliant)

```bash
# vLLM основной
LLM_BACKEND=vllm
VLLM_BASE_URL=http://vllm:8000
VLLM_MODEL_NAME=mistralai/Mistral-7B-Instruct-v0.2
LLM_VLLM_ENABLED=true
LLM_VLLM_PRIORITY=0

# Ollama резерный
LLM_OLLAMA_ENABLED=true
OLLAMA_MODEL=mistral:7b
LLM_OLLAMA_PRIORITY=1

# OpenAI отключен
LLM_OPENAI_ENABLED=false
```

### Development

```bash
# Ollama основной
LLM_BACKEND=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=mistral:7b
LLM_OLLAMA_ENABLED=true
LLM_OLLAMA_PRIORITY=0

# OpenAI fallback
LLM_OPENAI_ENABLED=true
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4
LLM_OPENAI_PRIORITY=1
```

## Потоковая генерация

```python
request = LLMRequest(
    messages=[
        ChatMessage(role=MessageRole.USER, content="Расскажи о KAG")
    ],
    stream=True
)

async for chunk in router.generate_stream(request):
    print(chunk.delta, end="", flush=True)
    if chunk.finish_reason:
        print("\nГенерация завершена")
```

## Health Checks

```python
# Ручная проверка
results = await router.health_check()
for backend_type, status in results.items():
    print(f"{backend_type}: healthy={status.healthy}")

# Фоновый мониторинг
await router.start_health_monitoring()  # Каждые 60 секунд
```

## Circuit Breaker

Роутер автоматически:
- Отключает бэкенд после 3 последовательных ошибок
- Пробует снова через 30 секунд (half-open state)
- Восстанавливает при успешной проверке

## Статистика

```python
stats = router.get_stats()
print(stats)
# {
#   "total_backends": 3,
#   "healthy_backends": 2,
#   "backends": {
#     "vllm": {
#       "stats": {...},
#       "priority": 0,
#       "circuit_open": false
#     }
#   }
# }
```

## Обработка ошибок

```python
from src.llm import (
    LLMError,
    LLMConnectionError,
    LLMTimeoutError,
    LLMFallbackError,
    NoBackendsAvailableError
)

try:
    response = await router.generate(request)
except NoBackendsAvailableError:
    print("Все бэкенды недоступны!")
except LLMFallbackError as e:
    print(f"Все бэкенды отказали: {e.attempted_backends}")
except LLMTimeoutError:
    print("Таймаут запроса")
except LLMError as e:
    print(f"Ошибка LLM: {e}")
```

## Создание специализированных клиентов

### OpenAI

```python
from src.llm.openai_client import create_openai_client

client = create_openai_client(
    api_key="sk-...",
    model="gpt-4"
)
```

### Azure OpenAI

```python
from src.llm.openai_client import create_azure_client

client = create_azure_client(
    api_key="azure-key",
    base_url="https://your-resource.openai.azure.com",
    model="gpt-4-deployment",
    api_version="2024-02-15-preview"
)
```

### DashScope/Qwen

```python
from src.llm.openai_client import create_dashscope_client

client = create_dashscope_client(
    api_key="dashscope-key",
    model="qwen-max"
)
```

## Docker Compose интеграция

```yaml
services:
  api:
    environment:
      - LLM_BACKEND=vllm
      - VLLM_BASE_URL=http://vllm-llm:8000
      - VLLM_MODEL_NAME=mistralai/Mistral-7B-Instruct-v0.2

  vllm-llm:
    image: vllm/vllm-openai:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    environment:
      - MODEL_NAME=mistralai/Mistral-7B-Instruct-v0.2
    ports:
      - "8002:8000"
    volumes:
      - models_cache:/models

  # Или для development
  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama_data:/root/.ollama
```

## 152-ФЗ Соответствие

Для соответствия 152-ФЗ:

✅ **Используйте**:
- vLLM на своем GPU сервере
- Ollama локально
- Любые бэкенды в вашем контуре

❌ **Не используйте**:
- OpenAI API (данные уходят в США)
- Другие внешние API без сертификации

## Production рекомендации

1. **Всегда используйте vLLM** как основной бэкенд
2. **Включите health checks** для мониторинга
3. **Настройте circuit breaker** для graceful degradation
4. **Мониторьте метрики** через Prometheus
5. **Используйте fallback** для отказоустойчивости

## API Reference

### LLMRequest

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| messages | List[ChatMessage] | Required | История сообщений |
| model | str | None | Модель (override) |
| temperature | float | 0.7 | Температура (0.0-2.0) |
| max_tokens | int | 4096 | Максимум токенов |
| top_p | float | 1.0 | Top-p sampling |
| stream | bool | False | Потоковая передача |
| stop | List[str] | None | Стоп-последовательности |
| seed | int | None | Seed для воспроизводимости |

### LLMResponse

| Поле | Тип | Описание |
|------|-----|----------|
| id | str | ID ответа |
| model | str | Использованная модель |
| choices | List[Dict] | Варианты ответов |
| usage | UsageInfo | Информация о токенах |
| backend | LLMBackendType | Бэкенд |
| generated_text | property | Сгенерированный текст |

## Troubleshooting

### "Нет доступных бэкендов"

```bash
# Проверьте конфигурацию
docker compose exec api python -c "from src.config import get_settings; print(get_settings().get_llm_router_config())"

# Проверьте health
curl http://vllm:8000/health
curl http://ollama:11434/
```

### "Модель не найдена"

```bash
# Для Ollama
docker compose exec ollama ollama pull mistral:7b

# Для vLLM
# Проверьте что MODEL_NAME в docker-compose правильный
```

### Таймауты

```bash
# Увеличьте таймаут в .env
VLLM_TIMEOUT=300.0
OLLAMA_TIMEOUT=300.0
```
