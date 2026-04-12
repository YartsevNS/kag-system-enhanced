# Настройка подключения к Ollama и управления моделями

## 📋 Обзор

Проект настроен для работы с **удаленным сервером Ollama** на `192.168.50.41:11434` с возможностью:
- ✅ Переключения между LLM моделями
- ✅ Выбора embedding моделей
- ✅ Подключения к vLLM (опционально)
- ✅ Управления моделями через веб-интерфейс

---

## 🔧 Конфигурация

### .env файл

```bash
# ===========================================
# LLM - Ollama (основной бэкенд)
# ===========================================
LLM_BACKEND=ollama
OLLAMA_BASE_URL=http://192.168.50.41:11434
OLLAMA_MODEL=mistral:7b
OLLAMA_TIMEOUT=120.0
OLLAMA_KEEP_ALIVE=30m
LLM_OLLAMA_ENABLED=true
LLM_OLLAMA_PRIORITY=0

# ===========================================
# Embedding модели (тот же сервер Ollama)
# ===========================================
EMBEDDING_BASE_URL=http://192.168.50.41:11434
EMBEDDING_MODEL=nomic-embed-text:latest
EMBEDDING_TIMEOUT=60.0
EMBEDDING_DIMENSIONS=768

# ===========================================
# vLLM (опционально, для production)
# ===========================================
VLLM_BASE_URL=http://vllm:8000
VLLM_MODEL_NAME=mistralai/Mistral-7B-Instruct-v0.2
LLM_VLLM_ENABLED=false
LLM_VLLM_PRIORITY=1
```

---

## 🌐 Админ-панель

### Доступ

После запуска приложения откройте:

```
http://localhost:8000/api/v1/admin/models/admin
```

### Возможности

1. **Просмотр статуса системы**
   - Активный бэкенд (Ollama/vLLM)
   - Текущая LLM модель
   - Текущая embedding модель
   - Здоровье всех компонентов

2. **Управление LLM моделями**
   - Список доступных моделей
   - Переключение между моделями
   - Активация нужного бэкенда

3. **Управление Embedding моделями**
   - Список embedding моделей
   - Переключение на другую модель

4. **Все модели Ollama**
   - Полный список загруженных моделей
   - Размеры моделей
   - Возможность удаления

5. **Загрузка новых моделей**
   - Поле ввода названия модели
   - Кнопка загрузки из Ollama registry
   - Примеры: `llama2:7b`, `codellama:latest`, `mistral:latest`

---

## 📦 Рекомендуемые модели

### LLM модели

```bash
# Быстрая, хорошее качество
mistral:7b

# Больше контекста
mistral:7b-instruct-v0.2-q4_K_M

# Код
codellama:7b-code

# Русскоязычная
saiga:7b

# Максимальное качество (требуется больше VRAM)
llama2:13b
mixtral:8x7b
```

### Embedding модели

```bash
# Рекомендуемая (по умолчанию)
nomic-embed-text:latest

# Альтернативы
all-minilm:22m
all-minilm:33m
mxbai-embed-large:latest
```

---

## 🚀 Быстрый старт

### 1. Проверить доступность Ollama

```bash
curl http://192.168.50.41:11434/api/tags
```

### 2. Загрузить нужные модели на сервер Ollama

```bash
# Подключиться к серверу Ollama
ssh user@192.168.50.41

# Загрузить модели
ollama pull mistral:7b
ollama pull nomic-embed-text:latest
ollama pull llama2:7b
```

### 3. Настроить .env

```bash
cp .env.example .env
# Отредактировать .env под ваши нужды
```

### 4. Запустить KAG

```bash
docker compose --profile dev up -d
```

### 5. Открыть админ-панель

```
http://localhost:8000/api/v1/admin/models/admin
```

---

## 🔌 API Endpoints

### Получить статус

```bash
curl http://localhost:8000/api/v1/admin/models/status
```

### Список LLM моделей

```bash
curl http://localhost:8000/api/v1/admin/models/llm
```

### Список embedding моделей

```bash
curl http://localhost:8000/api/v1/admin/models/embeddings
```

### Переключить LLM модель

```bash
curl -X POST http://localhost:8000/api/v1/admin/models/switch-llm \
  -H "Content-Type: application/json" \
  -d '{
    "backend_type": "ollama",
    "model_name": "llama2:7b"
  }'
```

### Переключить embedding модель

```bash
curl -X POST http://localhost:8000/api/v1/admin/models/switch-embedding \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "all-minilm:33m"
  }'
```

### Загрузить новую модель

```bash
curl -X POST http://localhost:8000/api/v1/admin/models/pull \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "codellama:7b"
  }'
```

---

## 🔄 Переключение на vLLM

### 1. Настроить vLLM сервер

```bash
# docker-compose.yml уже содержит конфигурацию
# Просто включите её в .env:

LLM_VLLM_ENABLED=true
VLLM_BASE_URL=http://vllm:8000
VLLM_MODEL_NAME=mistralai/Mistral-7B-Instruct-v0.2
```

### 2. Запустить vLLM

```bash
docker compose --profile prod up -d vllm-llm
```

### 3. Переключиться через админ-панель

Откройте `http://localhost:8000/api/v1/admin/models/admin` и выберите vLLM модель.

---

## 📊 Мониторинг

### Проверить здоровье Ollama

```bash
curl http://192.168.50.41:11434/
```

### Проверить здоровье KAG API

```bash
curl http://localhost:8000/api/v1/health
```

### Проверить статус моделей

```bash
curl http://localhost:8000/api/v1/admin/models/status | jq
```

---

## 🐛 Troubleshooting

### "Модель не найдена"

```bash
# Проверить доступные модели
curl http://192.168.50.41:11434/api/tags | jq

# Загрузить модель
curl http://192.168.50.41:11434/api/pull -d '{"name": "mistral:7b"}'
```

### "Ошибка подключения"

```bash
# Проверить доступность сервера
ping 192.168.50.41

# Проверить порт
nc -zv 192.168.50.41 11434

# Проверить firewall
sudo ufw status
```

### Embedding не работает

```bash
# Убедиться что embedding модель загружена
curl http://192.168.50.41:11434/api/tags | jq '.models[] | select(.name | contains("embed"))'

# Если нет - загрузить
ollama pull nomic-embed-text:latest
```

---

## 📝 Структура файлов

```
src/
├── llm/
│   ├── embeddings.py          # Embedding клиент для Ollama
│   ├── ollama_client.py       # LLM клиент для Ollama
│   ├── vllm_client.py         # LLM клиент для vLLM
│   └── router.py              # Роутер с fallback
├── api/
│   ├── services/
│   │   └── model_manager.py   # Менеджер моделей
│   └── routes/
│       └── admin_models.py    # Админ-панель
└── config.py                   # Конфигурация с embedding
```

---

## 🎯 Следующие шаги

1. ✅ Подключить Ollama к KAG
2. ✅ Настроить embedding модели
3. ✅ Создать админ-панель
4. ⏳ Интегрировать с Chat API (RAG pipeline)
5. ⏳ Интегрировать с Qdrant (векторный поиск)
6. ⏳ Завершить Upload API
