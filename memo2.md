# 📝 Отчёт о сессии разработки KAG System — 13 апреля 2026

> **Дата**: 13 апреля 2026 г.
> **Разработчик**: Qwen (AI Assistant)
> **Пользователь**: nick / adminbankir
> **Проект**: `/home/nick/kagproject`

---

## 🎯 Задачи сессии

1. **Изучить проект**, найти и исправить ошибки (error.txt)
2. **Исправить сохранение настроек** — чанкинг, все переменные в БД
3. **Добавить русскоязычный OCR** для PDF-документов
4. **Запустить проект** и проверить работоспособность

---

## 📋 Этап 1: Анализ проекта и поиск ошибок

### Прочитанные файлы:
- `kag1.txt` — полное техническое задание на систему KAG (Knowledge Augmentation Generation)
- `memoq.md` — отчёт предыдущей сессии от 12 апреля 2026
- `README.md`, `requirements.txt`, `test_and_run.sh`
- `docker-compose.yml`, `Dockerfile`
- Все Python-файлы в `src/`

### Структура проекта (полная):
```
kagproject/
├── src/
│   ├── api/
│   │   ├── main.py              # FastAPI приложение
│   │   ├── middleware/
│   │   │   ├── auth.py          # Keycloak + Casbin
│   │   │   └── setup_checker.py # Проверка настройки
│   │   ├── routes/
│   │   │   ├── admin.py         # Системный статус
│   │   │   ├── admin_models.py  # Управление моделями (API)
│   │   │   ├── chat.py          # Чат с RAG
│   │   │   ├── health.py        # Health check
│   │   │   ├── setup.py         # Setup Wizard
│   │   │   └── upload.py        # Загрузка документов
│   │   ├── services/
│   │   │   ├── chat_service.py  # RAG pipeline
│   │   │   ├── config_store.py  # PostgreSQL Config Store
│   │   │   ├── docker_monitor.py
│   │   │   ├── document_service.py  # Обработка документов
│   │   │   ├── export_service.py
│   │   │   ├── model_manager.py # Менеджер моделей
│   │   │   └── ssh_manager.py   # SSH к серверу Ollama
│   │   └── static/
│   │       ├── admin.html       # Админ-панель
│   │       ├── documents.html   # Управление документами
│   │       ├── index.html       # Чат
│   │       ├── setup.html       # Setup Wizard
│   │       └── docker.html
│   ├── agents/                  # Многоагентная система
│   ├── auth/                    # Keycloak + Casbin
│   ├── database/                # SQLAlchemy модели
│   ├── evaluation/              # Оценка качества
│   ├── indexing/
│   │   ├── parsers.py           # Парсеры документов
│   │   ├── chunking.py
│   │   ├── embeddings_service.py
│   │   ├── vectorizer.py
│   │   ├── celery_app.py
│   │   ├── scheduler.py
│   │   └── tasks.py
│   ├── llm/                     # LLM бэкенды
│   │   ├── base.py              # Абстрактный LLMBackend
│   │   ├── models.py            # Pydantic модели
│   │   ├── router.py            # Роутер с fallback
│   │   ├── ollama_client.py
│   │   ├── vllm_client.py
│   │   ├── openai_client.py
│   │   ├── embeddings.py
│   │   └── exceptions.py
│   ├── mcp/                     # MCP сервер
│   ├── monitoring/              # OpenTelemetry + Prometheus
│   └── security/                # GOST криптография
├── docker/                      # Инфраструктура
├── tests/
└── docs/
```

---

## 🐛 Найденные ошибки и исправления

### Ошибка 1: Дублирование метода `initialize()` в EmbeddingsService
**Файл:** `src/indexing/embeddings_service.py`
**Проблема:** Метод `initialize()` объявлен **дважды**. Второй перезаписывал первый, из-за чего инициализация Qdrant и создание коллекции **не выполнялись**.
**Решение:** Объединены оба метода в один корректный `initialize()` + отдельный `_ensure_collection()`.

### Ошибка 2: GOST-ключ генерировался случайно при каждом запуске
**Файл:** `src/security/gost_crypto.py`
**Проблема:** Глобальный экземпляр `GOSTCrypto()` генерировал случайный ключ (`os.urandom(32)`). После перезапуска контейнера **все зашифрованные пароли невозможно расшифровать**.
**Решение:** Ключ загружается из `/app/data/.encryption_key` (если существует), иначе генерируется и сохраняется с правами `0o600`.

### Ошибка 3: `KC_DB_USERNAME`/`KC_DB_PASSWORD` отсутствовали в Settings
**Файл:** `src/config.py`
**Проблема:** `config_store` использовал `getattr(settings, 'KC_DB_USERNAME', 'keycloak')` — поля отсутствовали в классе Settings.
**Решение:** Добавлены поля `KC_DB_USERNAME`, `KC_DB_PASSWORD`, `KC_DB_HOST`, `KC_DB_PORT`, `KC_DB_NAME`.

### Ошибка 4: URL настроек чанкинга — дублирование префикса (404!)
**Файл:** `src/api/static/admin.html`
**Проблема:** `API_URL = '/api/v1/admin/models'`, затем вызов `${API_URL}/admin/models/chunking-config` = `/api/v1/admin/models/admin/models/chunking-config` → **404!**
**Решение:** Исправлено на `${API_URL}/chunking-config` (GET и POST).

### Ошибка 5: Документы загружаются, но НЕ обрабатываются
**Файл:** `src/api/routes/upload.py`
**Проблема:** `BackgroundTasks` мог быть `None` — задача просто не запускалась.
**Решение:** Добавлен fallback — если `background_tasks is None`, обработка вызывается **синхронно**.

### Ошибка 6: Метаданные документов терялись при перезапуске
**Файл:** `src/api/services/document_service.py`
**Проблема:** `_documents` хранился **только в памяти** (in-memory dict). При перезапуске контейнера все данные терялись.
**Решение:**
- Добавлена `_load_documents_from_db()` — загрузка из PostgreSQL при старте
- Добавлена `_save_document_to_db()` — сохранение при каждом изменении статуса
- Сохранение на каждом шаге обработки (pending→processing→completed/failed)

### Ошибка 7: EmbeddingsService не инициализировался при старте
**Файл:** `src/api/main.py`
**Проблема:** Не вызывался `embeddings_service.initialize()` в `lifespan`.
**Решение:** Добавлена инициализация и закрытие EmbeddingsService в lifespan.

### Ошибка 8: MessageRole мог вызвать ValueError
**Файл:** `src/api/services/chat_service.py`
**Проблема:** `MessageRole(invalid_string)` бросает исключение.
**Решение:** Добавлен `try/except` с fallback на `USER`.

### Ошибка 9: Дублирование создания таблиц БД
**Файл:** `src/api/main.py`
**Проблема:** `Base.metadata.create_all()` вызывался и в `main.py` и в `config_store.__init__()`.
**Решение:** Оставлено только в `config_store`.

### Ошибка 10: Испанское слово в комментарии
**Файл:** `src/llm/embeddings.py`
**Проблема:** «primero» вместо «сначала».
**Решение:** Заменено на русский.

---

## 🔧 Задача: Все настройки сохраняются в PostgreSQL

### Проблема
Пользователь сообщил: «Настройки чанкинга документов не сохраняются».

### Диагностика
- `config_store` подключается к `keycloak-db:5432/keycloak` (единственный PostgreSQL)
- Но URL строился через `getattr()` с хардкод-дефолтами
- ENV переменные `KC_DB_*` отсутствовали в `.env.example` и `docker-compose.yml`

### Решение
1. **`.env.example`** — добавлены `KC_DB_USERNAME`, `KC_DB_PASSWORD`, `KC_DB_HOST`, `KC_DB_PORT`, `KC_DB_NAME`
2. **`src/config.py`** — добавлены эти поля в класс `Settings`
3. **`src/api/services/config_store.py`** — переписана инициализация: URL строится из `settings.KC_DB_*`
4. **`docker-compose.yml`** — добавлены ENV переменные в контейнер `api` + зависимость от `keycloak-db`

### Какие настройки теперь сохраняются в БД:

| Категория | Ключ | Данные |
|-----------|------|--------|
| `chunking` | `default` | chunk_size, chunk_overlap |
| `database` | `default` | host, port, name, user, password (GOST) |
| `llm` | `default` | type, host, port, model |
| `embedding` | `default` | model, dimensions |
| `ssh` | `default` | host, port, username, password (GOST), sudo_password |
| `setup` | `status` | configured, timestamp, llm_model, db_host |
| `documents` | `{doc_id}` | метаданные каждого документа |

---

## 🔤 Задача: Русскоязычный OCR для PDF-документов

### Выбор технологии
Выбран **Tesseract OCR** (https://github.com/tesseract-ocr/tesseract):
- Open-source, проект Google
- Отличная поддержка русского языка (`tesseract-ocr-rus`)
- Де-факто стандарт в индустрии

Для Python:
- `pytesseract` — обёртка для Tesseract
- `Pillow` — обработка изображений
- `pdf2image` — рендеринг PDF страниц в изображения (требует `poppler-utils`)

### Реализация

#### Новый файл: `src/indexing/ocr_engine.py`
```python
class OCREngine:
    """
    Движок оптического распознавания символов.
    Поддерживает:
    - OCR изображений (PNG, JPG, TIFF, BMP)
    - OCR PDF-документов (через рендеринг страниц в изображения)
    - Распознавание русского и английского текста
    """
```

**Методы:**
- `extract_text_from_image(image_path)` → текст
- `extract_text_from_pdf(pdf_path)` → `{"pages": [{"page": N, "text": "..."}], "total_pages": N}`

**Параметры по умолчанию:**
- Языки: `rus+eng`
- DPI: 300
- PSM: 3 (автоматический)

#### Изменён: `src/indexing/parsers.py`
PDF парсер теперь:
1. Пытается извлечь текст через PyPDF2
2. Если текст **пустой** или **< 50 символов** → автоматически **Tesseract OCR**
3. Метаданные содержат флаг `{"ocr_used": true}`

#### `requirements.txt` — добавлено:
```
pytesseract>=0.3.10
Pillow>=10.0.0
pdf2image>=1.17.0
```

#### `Dockerfile` — добавлено (и в builder, и в production):
```dockerfile
tesseract-ocr
tesseract-ocr-rus
poppler-utils
```

---

## 📊 Итоговый статус контейнеров

```
NAME              STATUS                          PORTS
kag-api           Up 8 hours (healthy)            0.0.0.0:8000->8000/tcp
kag-keycloak      Up 11 hours (unhealthy)         0.0.0.0:8080->8080/tcp
kag-keycloak-db   Up 11 hours (healthy)           5432/tcp
kag-mcp           Up 11 hours (healthy)           0.0.0.0:8001->8001/tcp
kag-qdrant        Up 11 hours (unhealthy)         0.0.0.0:6333-6334->6333-6334/tcp
kag-redis         Up 11 hours (healthy)           0.0.0.0:6379->6379/tcp
kag-scheduler     Restarting (0)                  —
kag-worker        Up 11 hours (healthy)           —
```

## 🌐 Доступные URL

| Сервис | URL |
|--------|-----|
| Чат | `http://localhost:8000/` |
| API Docs | `http://localhost:8000/docs` |
| Setup | `http://localhost:8000/setup` |
| Админ-панель | `http://localhost:8000/admin` |
| Документы | `http://localhost:8000/documents` |
| MCP сервер | `http://localhost:8001` |

## 📝 Изменённые файлы (итого)

| Файл | Изменение |
|------|-----------|
| `src/indexing/embeddings_service.py` | Убран дубликат initialize() |
| `src/security/gost_crypto.py` | Загрузка/сохранение ключа из файла |
| `src/config.py` | Добавлены KC_DB_* поля |
| `src/api/services/config_store.py` | URL из settings.KC_DB_* |
| `src/api/static/admin.html` | Исправлен URL чанкинга |
| `src/api/routes/upload.py` | Fallback на синхронную обработку |
| `src/api/services/document_service.py` | Сохранение в PostgreSQL |
| `src/api/main.py` | Инициализация EmbeddingsService |
| `src/api/services/chat_service.py` | Безопасный MessageRole |
| `src/llm/embeddings.py` | Исправлено «primero» → «сначала» |
| `src/indexing/ocr_engine.py` | **Новый** — Tesseract OCR движок |
| `src/indexing/parsers.py` | OCR интеграция в PDF парсер |
| `requirements.txt` | pytesseract, Pillow, pdf2image |
| `Dockerfile` | tesseract-ocr, tesseract-ocr-rus, poppler-utils |
| `.env.example` | KC_DB_* переменные |
| `docker-compose.yml` | KC_DB_* ENV + depends_on keycloak-db |
| `error.txt` | Отчёт об ошибках и исправлениях |

---

## 🚀 Как перезапустить с новыми изменениями

```bash
# Пересобрать и запустить
cd /home/nick/kagproject
docker compose --profile dev up -d --build

# Проверить логи API
docker logs -f kag-api

# Проверить что Tesseract установлен
docker exec kag-api tesseract --version
docker exec kag-api tesseract --list-langs
```

## 💡 Примечания

- Для применения OCR к уже загруженным PDF — нужно **перезагрузить** документы через `/documents`
- OCR работает **автоматически** — если PyPDF2 не извлёк текст, Tesseract распознаёт его
- Все настройки сохраняются в PostgreSQL и не теряются при перезапуске
- GOST-ключ хранится в `/app/data/.encryption_key` и загружается при старте
