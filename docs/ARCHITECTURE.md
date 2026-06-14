# KAG System — Архитектура проекта

> **KAG (Knowledge Augmentation Generation)** — AI-система управления документами с RAG (Retrieval-Augmented Generation).  
> Версия: 0.3.0 | Ветка: feature/docling-integration | Сервер: 192.168.50.18

---

## 1. Оглавление

1. [Общая архитектура](#2-общая-архитектура)
2. [Контейнеры и инфраструктура](#3-контейнеры-и-инфраструктура)
3. [Поток данных: от загрузки до поиска](#4-поток-данных-от-загрузки-до-поиска)
4. [Структура исходного кода](#5-структура-исходного-кода)
5. [Frontend: страницы и их назначение](#6-frontend-страницы-и-их-назначение)
6. [API: эндпоинты и маршруты](#7-api-эндпоинты-и-маршруты)
7. [Безопасность и шифрование](#8-безопасность-и-шифрование)
8. [Базы данных и хранение](#9-базы-данных-и-хранение)
9. [Qdrant: векторный поиск](#10-qdrant-векторный-поиск)
10. [Анализатор документов](#11-анализатор-документов)
11. [Развёртывание](#12-развёртывание)

---

## 2. Общая архитектура

```
┌─────────────────────────────────────────────────────────┐
│                    Пользователь                          │
│  Браузер → https://qd.gostsecret.ru (Nginx :80/:443)    │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  Nginx (kag-nginx) — обратный прокси                     │
│  Проксирует: / → kag-api:8000                           │
│  Статика: /static/ → встроена в kag-api                 │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│  API (kag-api) — FastAPI + Uvicorn :8000                 │
│  • Загрузка/обработка документов                         │
│  • Поиск по чанкам (RAG)                                 │
│  • Чат с AI                                              │
│  • Управление моделями                                   │
│  • Админ-панель                                          │
└──┬──────────┬──────────┬──────────┬─────────────────────┘
   │          │          │          │
   ▼          ▼          ▼          ▼
┌──────┐ ┌──────┐ ┌────────┐ ┌──────────────┐
│Qdrant│ │Redis │ │Keycloak│ │Keycloak-DB   │
│:6333 │ │:6379 │ │:8080   │ │PostgreSQL:5432│
│вектор│ │кэш + │ │Auth    │ │Пользователи,  │
│ БД   │ │брокер│ │OIDC    │ │config_store   │
└──────┘ └──────┘ └────────┘ └──────────────┘
   │
   ▼
┌──────────────────────────────────────┐
│  Ollama (192.168.50.41:11434)        │
│  • phi4-mini (LLM)                   │
│  • nomic-embed-text (Embeddings 768d)│
│  • 40+ моделей доступно              │
└──────────────────────────────────────┘
```

---

## 3. Контейнеры и инфраструктура

### 3.1. Профили Docker Compose

| Профиль | Контейнеры |
|---------|-----------|
| `dev` (по умолчанию) | api, worker, mcp-server, qdrant, redis, keycloak, keycloak-db, scheduler |
| `prod` | всё из dev + nginx |
| `monitoring` | prometheus, grafana, loki, otel-collector |

Запуск: `docker-compose --profile dev up -d`

### 3.2. Контейнеры

| Контейнер | Образ | Порт | CPU | RAM | Назначение |
|-----------|-------|------|-----|-----|-----------|
| **kag-api** | Dockerfile | 8000 | 2.0 | 2G | FastAPI-сервер, основной API |
| **kag-worker** | Dockerfile.worker | — | 2.0 | 4G | Celery-воркер для фоновых задач |
| **kag-mcp** | Dockerfile.mcp | 8001 | 2.0 | 2G | MCP-сервер для тестирования |
| **kag-qdrant** | qdrant/qdrant:v1.12.1 | 6333-6334 | 4.0 | 8G | Векторная БД |
| **kag-redis** | redis:7-alpine | 6379 | 2.0 | 2G | Кэш + брокер Celery (maxmemory 2GB, allkeys-lru) |
| **kag-keycloak** | keycloak:24.0 | 8080 | 2.0 | 4G | Identity Provider (OIDC) |
| **kag-keycloak-db** | postgres:16-alpine | 5432 | 2.0 | 2G | PostgreSQL для Keycloak + config_store |
| **kag-scheduler** | Dockerfile | — | 1.0 | 1G | Планировщик задач (APScheduler) |
| **kag-nginx** | nginx:1.25-alpine | 80,443 | 1.0 | 512M | Прокси (только prod) |

### 3.3. Docker-тома (bind mounts)

| Хост | Контейнер | Назначение |
|------|-----------|-----------|
| `./src` | `/app/src` | Исходный код (live-reload) |
| `./data` | `/app/data` | Данные: uploads, thumbnails, audit, encryption_key |
| `./user_data` | `/app/user_data` | Пользовательские файлы: originals, uploads |

### 3.4. Сеть

Все контейнеры в сети `kag_internal` (bridge). API доступен снаружи через проброс порта 8000 (dev) или через Nginx (prod).

### 3.5. Переменные окружения (ключевые)

| Переменная | Значение | Назначение |
|-----------|----------|-----------|
| `OLLAMA_BASE_URL` | `http://192.168.50.41:11434` | Сервер Ollama |
| `EMBEDDING_MODEL` | `nomic-embed-text:latest` | Модель эмбеддингов (768d) |
| `KC_DB_USERNAME` / `KC_DB_PASSWORD` | keycloak / *** | Доступ к PostgreSQL |
| `AUTH_ENABLED` | false | Аутентификация (JWT + cookie) |

---

## 4. Поток данных: от загрузки до поиска

### 4.1. Загрузка документа

```
POST /api/v1/upload/  (multipart/form-data)
         │
         ▼
┌──────────────────────────────────────┐
│ 1. upload_document()                 │  upload.py:20
│    • Валидация (SecurityValidator)    │
│    • Сохранение файла на диск        │  storage_service.py
│      → /app/user_data/uploads/       │
│    • Создание записи в config_store   │  system_configs (PostgreSQL)
│    • Запуск process_document() в фоне│  BackgroundTasks
└──────────────────────────────────────┘
         │ (фон)
         ▼
┌──────────────────────────────────────┐
│ 2. process_document()                │  document_service.py:205
│    Прогресс: 10% → 30% → 50% → 90% → 100%
│                                       │
│    [30%] Парсинг                      │
│    • document_parser.parse()          │  parsers.py
│    • PDF → pdfplumber + Docling       │
│    • DOCX → python-docx              │
│    • OCR: Tesseract + Occular-ocr    │  ocr_engine.py
│    • Возвращает segments[]           │
│                                       │
│    [50%] Чанкинг                      │
│    • DocumentChunker.chunk()          │  chunking.py
│    • Размер: 1000 символов            │
│    • Перекрытие: 200 символов         │
│    • chunk_id: chunk_00001, ...      │
│    • chunk_seq: 1, 2, 3...           │
│                                       │
│    [90%] Векторизация                 │
│    • EmbeddingsService                │  embeddings_service.py
│    • nomic-embed-text (768d)          │  → Ollama API
│    • Сохранение в Qdrant             │  коллекция kag_documents
│      payload: {document_id, chunk_id, │
│                text, chunk_seq, ...}  │
│                                       │
│    [100%] Завершение                  │
│    • Миниатюра (WebP, 500px)          │
│    • Статус: completed               │
│                                       │
│    [ФОН] Анализ первого чанка         │
│    • DocumentAnalyzer                 │  document_analyzer.py
│    • LLM (phi4-mini) → title, type,   │
│      summary, topics                 │
│    • Сохранение в config_store +      │
│      обновление Qdrant payload       │
└──────────────────────────────────────┘
```

### 4.2. Поиск (RAG)

```
POST /api/v1/chat/search  {"query": "...", "filters": {...}}
         │
         ▼
┌──────────────────────────────────────┐
│ 1. Векторизация запроса              │
│    • nomic-embed-text → vector[768]  │
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│ 2. Поиск в Qdrant                    │
│    • Cosine distance                 │
│    • HNSW (m=32, ef_construct=128)   │
│    • INT8 квантование               │
│    • Фильтр: document_id, file_type  │
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│ 3. Сборка контекста                  │
│    • top-K чанков (по умолчанию 5)   │
│    • Сортировка по chunk_seq         │
│    • Сборка в промпт                 │
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│ 4. Генерация ответа                  │
│    • phi4-mini (или другая LLM)      │
│    • Контекст: чанки + системный     │
│      промпт                          │
│    • Стриминг ответа                 │
└──────────────────────────────────────┘
```

---

## 5. Структура исходного кода

```
kag-system-enhanced/
├── docker-compose.yml          # Оркестрация контейнеров (dev/prod/monitoring)
├── Dockerfile                  # Основной образ (FastAPI)
├── Dockerfile.worker           # Celery-воркер
├── Dockerfile.mcp              # MCP-сервер
├── requirements.txt            # Python-зависимости
├── .env                        # Переменные окружения
├── data/                       # Данные на хосте (uploads, thumbnails, audit)
├── user_data/                  # Пользовательские файлы
├── docker/                     # Конфиги Nginx, Keycloak, Prometheus, Grafana
│
└── src/
    ├── config.py               # Настройки (Settings: pydantic BaseSettings)
    ├── models.py               # Pydantic-модели (DocumentUpload, SystemStatus, ...)
    │
    ├── api/                    # === FastAPI-приложение ===
    │   ├── main.py             # Точка входа: app = FastAPI(), монтирование роутеров
    │   ├── middleware/         # Промежуточные слои
    │   │   ├── auth.py         # Базовая auth (устарела)
    │   │   ├── auth_v2.py      # JWT + cookie auth (текущая)
    │   │   ├── auth_gate.py    # Auth gate: редирект на /login
    │   │   └── setup_checker.py# Проверка что setup wizard пройден
    │   ├── routes/             # API-эндпоинты
    │   │   ├── upload.py       # Загрузка, детали, чанки, превью, миниатюры
    │   │   ├── chat.py         # Чат + поиск (RAG)
    │   │   ├── admin.py        # Статус системы
    │   │   ├── admin_models.py # Управление LLM/Embedding моделями + внешние LLM
    │   │   ├── auth.py         # Логин/логаут
    │   │   ├── setup.py        # Мастер настройки
    │   │   ├── watchers.py     # Web- и folder-наблюдатели
    │   │   ├── notifications.py# Уведомления
    │   │   └── health.py       # Health-check
    │   ├── services/           # Бизнес-логика
    │   │   ├── document_service.py    # Загрузка → чанкинг → векторизация
    │   │   ├── document_analyzer.py   # LLM-анализ первого чанка → метаданные
    │   │   ├── chat_service.py        # Чат + RAG-поиск
    │   │   ├── config_store.py        # PostgreSQL key-value хранилище (system_configs)
    │   │   ├── storage_service.py     # Файловое хранилище (SHA-256, hash-based naming)
    │   │   ├── model_manager.py       # Управление Ollama-моделями
    │   │   ├── docker_monitor.py      # Мониторинг Docker
    │   │   ├── ssh_manager.py         # SSH-подключения
    │   │   ├── export_service.py      # Экспорт документов
    │   │   ├── system_monitor.py      # Системный мониторинг
    │   │   ├── qdrant_monitor.py      # Мониторинг Qdrant
    │   │   └── notification_service.py# Сервис уведомлений
    │   └── static/            # HTML/CSS/JS страницы (см. раздел 6)
    │
    ├── indexing/              # === Индексация документов ===
    │   ├── chunking.py        # DocumentChunker: разбивка текста на чанки
    │   ├── embeddings_service.py # EmbeddingsService: векторизация + Qdrant
    │   ├── qdrant_service.py  # QdrantService: REST-клиент для Qdrant
    │   ├── parsers.py         # DocumentParser: PDF, DOCX, TXT, CSV
    │   ├── hybrid_parser.py   # Docling + Occular-ocr гибрид
    │   ├── ocr_engine.py      # Tesseract + Occular-ocr
    │   ├── llm_ocr.py         # LLM-based OCR
    │   ├── auto_tagger.py     # Авто-классификатор (8 типов документов)
    │   ├── vectorizer.py      # Векторизатор текста
    │   ├── celery_app.py      # Celery-приложение
    │   ├── scheduler.py       # APScheduler
    │   └── tasks.py           # Celery-задачи
    │
    ├── llm/                   # === LLM-клиенты ===
    │   ├── base.py            # Базовый класс LLMClient
    │   ├── ollama_client.py   # Ollama API клиент
    │   ├── openai_client.py   # OpenAI API клиент
    │   ├── vllm_client.py     # vLLM API клиент
    │   ├── embeddings.py      # EmbeddingClient: генерация эмбеддингов
    │   ├── router.py          # LLMRouter: маршрутизация между провайдерами
    │   ├── models.py          # Модели данных LLM
    │   └── exceptions.py      # Исключения
    │
    ├── database/              # === Базы данных ===
    │   ├── models.py          # Базовые модели (SystemConfig, Base)
    │   ├── document_models.py # Document, DocumentVersion (SQLAlchemy)
    │   ├── user_models.py     # User, Group (Keycloak-совместимые)
    │   ├── monitoring_models.py # Модели мониторинга
    │   └── session.py         # Сессии БД
    │
    ├── security/              # === Безопасность ===
    │   ├── gost_crypto.py     # GOST-шифрование (см. раздел 8)
    │   ├── validator.py       # Валидатор файлов (SecurityValidator)
    │   └── audit.py           # Аудит-логгер
    │
    ├── auth/                  # === Авторизация ===
    │   ├── keycloak.py        # Keycloak OIDC-клиент
    │   ├── casbin.py          # Casbin RBAC
    │   └── permissions.py     # Модели прав доступа
    │
    ├── monitoring/            # === Мониторинг ===
    │   ├── prometheus.py      # Prometheus-метрики
    │   ├── opentelemetry.py   # OpenTelemetry-трейсинг
    │   ├── folder_watcher.py  # Наблюдатель папок (авто-импорт)
    │   └── web_watcher.py     # Web-наблюдатель
    │
    ├── agents/                # === AI-агенты (планирование) ===
    │   ├── planner.py         # Планировщик задач
    │   ├── executor.py        # Исполнитель
    │   └── evaluator.py       # Оценщик качества
    │
    ├── evaluation/            # === Оценка качества ===
    │   ├── ab_testing.py      # A/B-тестирование
    │   └── quality.py         # Метрики качества
    │
    └── mcp/                   # === MCP-сервер ===
        ├── server.py          # Model Context Protocol сервер
        └── tools.py           # MCP-инструменты
```

---

## 6. Frontend: страницы и их назначение

Все страницы в `src/api/static/`. Linear Dark Theme (#08090a, accent #5e6ad2, Inter).

| Страница | Файл | Назначение | Ключевые функции |
|----------|------|-----------|-----------------|
| **Дашборд** | index.html | Главная страница, сводка | Статистика, статус системы |
| **Документы** | documents.html | Загрузка, список, фильтрация | Drag&drop, прогресс-бар, карточки A4, поиск |
| **Просмотр PDF** | viewer.html | Canvas-просмотрщик | PDF.js 3.11, зум, клавиатура, постранично |
| **Чанки** | chunks.html | Просмотр чанков документа | Пагинация, limit-селектор, Чанк #N |
| **Поиск** | search.html | Семантический поиск | RAG + фильтры |
| **Чат с AI** | chat.html | Диалог с LLM | Стриминг, контекст из документов |
| **OCR-демо** | ocr.html | Тест OCR-движка | Occular-ocr, Tesseract |
| **Логи** | logs.html | Системные логи | Фильтрация, поиск |
| **Мониторинг** | monitoring.html | Метрики системы | Prometheus, Grafana |
| **Пользователи** | users.html | Управление пользователями | Keycloak-интеграция |
| **Админ** | admin.html | Управление моделями | Ollama pull/delete, внешние LLM |
| **Qdrant** | qdrant.html | Просмотр Qdrant | Коллекции, точки |
| **Docker** | docker.html | Мониторинг контейнеров | Статусы, логи |
| **Вход** | login.html | Аутентификация | JWT + cookie |
| **Setup** | setup.html | Мастер первой настройки | Генерация паролей, GOST |

---

## 7. API: эндпоинты и маршруты

### 7.1. Документы (`/api/v1/upload/`)

| Метод | Путь | Назначение | Ключевые поля ответа |
|-------|------|-----------|---------------------|
| POST | `/` | Загрузить документ | document_id, status |
| GET | `/list` | Список документов | documents[], total |
| GET | `/{id}/details` | Детали (тип, тэги, чанки) | document_type, tags, chunks_count, recognized_title |
| GET | `/{id}/chunks` | Чанки документа (пагинация) | chunks[], total |
| GET | `/{id}/preview` | Файл для просмотра (inline) | FileResponse (PDF) |
| GET | `/{id}/thumbnail` | Миниатюра (WebP) | FileResponse |
| GET | `/{id}/status` | Статус обработки | status, progress |
| DELETE | `/{id}` | Удалить документ | — |

### 7.2. Чат и поиск (`/api/v1/chat/`)

| Метод | Путь | Назначение |
|-------|------|-----------|
| POST | `/search` | Семантический поиск по чанкам |
| POST | `/ask` | Задать вопрос с контекстом (RAG) |
| GET | `/history` | История диалогов |

### 7.3. Админ (`/api/v1/admin/`, `/api/v1/admin/models/`)

| Метод | Путь | Назначение |
|-------|------|-----------|
| GET | `/status` | Статус системы |
| GET | `/models/status` | Статус LLM/Embedding |
| GET | `/models/list` | Список моделей Ollama |
| POST | `/models/pull` | Загрузить модель |
| DELETE | `/models/{name}` | Удалить модель |
| POST | `/models/switch` | Переключить активную модель |
| GET/POST | `/models/ext-llm` | Внешний LLM для анализа |
| POST | `/models/ext-llm/test` | Тест внешнего LLM |

### 7.4. Auth (`/api/v1/auth/`)

| Метод | Путь | Назначение |
|-------|------|-----------|
| POST | `/login` | JWT-логин |
| POST | `/logout` | Выход |
| GET | `/me` | Текущий пользователь |

---

## 8. Безопасность и шифрование

### 8.1. Библиотеки шифрования

| Библиотека | Версия | Назначение |
|-----------|--------|-----------|
| **cryptography** | 44.0.0 | Криптографические примитивы (AES, хэши) |
| **python-jose** | 3.3.0 | JWT-токены (HS256/RS256) |
| **passlib[bcrypt]** | 1.7.4 | Хэширование паролей (bcrypt) |
| **casbin** | 1.36.0 | RBAC-авторизация |

### 8.2. GOST-шифрование (`src/security/gost_crypto.py`)

**Назначение:** шифрование конфиденциальных данных (пароли, ключи) в соответствии с ГОСТ Р 34.12-2015 для совместимости с 152-ФЗ.

**Реализация:**
- **Шифрование**: ГОСТ Р 34.12-2015 (Kuznyechik — 128-битный блочный шифр, Magma — 64-битный) через `cryptography.hazmat.primitives.ciphers`
- **Хэширование**: ГОСТ Р 34.11-2012 (Streebog — 256/512 бит) через `hashlib`
- **Режим**: CBC (Cipher Block Chaining)
- **Ключ**: хранится в `/app/data/.encryption_key` (256 бит), генерируется при первом запуске
- **Использование**: `GOSTCrypto.encrypt(data)`, `GOSTCrypto.decrypt(data)`

**Класс `GOSTCrypto`:**
```python
class GOSTCrypto:
    def encrypt(self, plaintext: str) -> str      # → base64
    def decrypt(self, ciphertext: str) -> str     # ← base64
    def hash_streebog(self, data: bytes) -> str   # hex-digest
    def generate_key(self) -> bytes               # 32 байта
```

### 8.3. Валидация файлов (`src/security/validator.py`)

**SecurityValidator** проверяет:
- Размер файла (макс. 50MB для PDF, 10MB для остальных)
- MIME-тип (whitelist: PDF, DOCX, TXT, MD, CSV)
- Content-type заголовок
- Имя файла (без path traversal)

### 8.4. Аудит (`src/security/audit.py`)

**AuditLogger** пишет в `/app/data/audit/audit.log`:
- Загрузка/удаление документов
- Изменение настроек
- Попытки входа

### 8.5. Аутентификация

- **JWT** (python-jose): токены с временем жизни
- **Cookie**: `kag_token` + `localStorage`
- **Keycloak OIDC**: опционально (AUTH_ENABLED=false по умолчанию)
- **RBAC**: Casbin (роли: admin, user)

---

## 9. Базы данных и хранение

### 9.1. PostgreSQL (keycloak-db)

**Таблицы KAG:**

| Таблица | Назначение | Ключевые поля |
|---------|-----------|--------------|
| `system_configs` | Key-value хранилище (config_store) | id (category:key), category, key, value (JSON) |
| `documents` | Документы (SQLAlchemy, задел) | id, filename, file_hash, status, uploaded_by |
| `users` | Пользователи (Keycloak-совместимые) | id, username, email |
| `groups` | Группы | id, name |

**config_store** (`src/api/services/config_store.py`):
- Ключи: `{category}:{key}`, например `documents:445e6794-...`
- Категории: `documents`, `chunking`, `features`
- Значения: JSON-сериализованные dict
- Подключение: `postgresql://keycloak:***@keycloak-db:5432/keycloak`

### 9.2. Redis

| БД | Назначение |
|----|-----------|
| 0 | Кэш (по умолчанию) |
| 1 | Celery broker |
| 2 | Celery result backend |

Политика: `allkeys-lru`, maxmemory 2GB.

### 9.3. Файловое хранилище

**StorageService** (`src/api/services/storage_service.py`):
- Путь: `/app/user_data/originals/{doc_id[:2]}/{doc_id}_{filename}`
- Хэширование: SHA-256 для проверки целостности
- Методы: `store_original()`, `get_original()`, `delete_original()`

**Загрузки**: `/app/data/uploads/{doc_id}_{filename}`
**Миниатюры**: `/app/data/thumbnails/{doc_id}.webp` (500px, WebP)

---

## 10. Qdrant: векторный поиск

### 10.1. Конфигурация коллекции

| Параметр | Значение | Обоснование |
|----------|---------|------------|
| Коллекция | `kag_documents` | — |
| Размерность | 768 | nomic-embed-text |
| Distance | Cosine | Лучше для текстов |
| HNSW m | 32 | Больше связей = точнее |
| HNSW ef_construct | 128 | Качественнее построение |
| Квантование | INT8 scalar | Сжатие в 4 раза, быстрее поиск |
| Payload on_disk | true | Экономия RAM |
| Индексация | после 5000 точек | Групповая оптимизация |

### 10.2. Payload-индексы

Для быстрой фильтрации созданы индексы на поля:
- `document_id` (keyword)
- `chunk_id` (keyword)
- `file_type` (keyword)
- `group_ids` (keyword)

### 10.3. Структура точки в Qdrant

```json
{
  "id": "uuid",
  "vector": [0.123, -0.456, ...],  // 768d
  "payload": {
    "document_id": "445e6794-...",
    "chunk_id": "chunk_00001",
    "chunk_seq": 1,
    "text": "Текст чанка...",
    "filename": "документ.pdf",
    "file_type": "application/pdf",
    "document_type": "contract",
    "summary": "Договор на оказание услуг...",
    "topics": ["договор", "услуги"]
  }
}
```

---

## 11. Анализатор документов

### 11.1. DocumentAnalyzer (`src/api/services/document_analyzer.py`)

**Назначение:** фоновый анализ первого чанка через LLM для извлечения метаданных.

**Процесс:**
1. После завершения чанкинга → `asyncio.create_task(_analyze_document_async)`
2. Первый чанк → LLM (phi4-mini на 192.168.50.41)
3. Промпт: «Проанализируй начало документа и верни JSON»
4. Ответ: `{title, type, summary, topics}`
5. Сохранение: config_store + Qdrant payload

**Типы документов (10):** invoice, contract, report, letter, form, identity, medical, legal, financial, technical, other

**Настройка:** через админку → раздел «Внешние LLM» (URL, модель, провайдер)

---

## 12. Развёртывание

### 12.1. Быстрый старт (dev)

```bash
cd /home/yartsevn/kag-system
git pull
docker-compose --profile dev up -d --no-deps --build api
```

### 12.2. Проверка

```bash
# Статус контейнеров
docker ps --format '{{.Names}} {{.Status}}'

# Health-check API
curl http://localhost:8000/api/v1/health

# Qdrant
curl http://localhost:6333/collections/kag_documents
```

### 12.3. Адреса

| Сервис | Dev | Prod |
|--------|-----|------|
| API | http://192.168.50.18:8000 | https://qd.gostsecret.ru |
| Qdrant | http://192.168.50.18:6333 | (внутренний) |
| Admin | http://192.168.50.18:8000/admin | https://qd.gostsecret.ru/admin |
| Документы | http://192.168.50.18:8000/documents | https://qd.gostsecret.ru/documents |

### 12.4. SSH

```bash
ssh yartsevn@192.168.50.18  # локальная сеть
ssh yartsevn@37.204.20.233  # внешний IP
```

---

> **Последнее обновление:** 15 мая 2026  
> **Ветка:** feature/docling-integration (20+ коммитов)  
> **Автор:** KAG Team / YartsevNS
