# Code Review: KAG Project

## Общая оценка: 5/10

Проект на ранней стадии (MVP/прототип). Архитектура заложена разумно, но в текущем виде **непригоден для production** из-за критических проблем безопасности и незавершённости.

---

## 1. БЕЗОПАСНОСТЬ — Оценка: 3/10 (Критично)

### Критические проблемы

**1.1. API-ключи в git** (`api_key.txt:1`)

Файл `api_key.txt` с реальными ключами Qwen (`sk-a7f449719f...`, `sk-24545ef027...`) отслеживается в git. Это утечка credentials.

- Удалить файл из git: `git rm --cached api_key.txt`
- Добавить `api_key.txt` в `.gitignore`
- Перегенерировать скомпрометированные ключи
- Переместить секреты в `.env` (который уже в `.gitignore`)

**1.2. Shell injection в SSH Manager** (`ssh_manager.py:156-164`)

Пароли и параметры подставляются прямо в shell-команду через f-строку:

```python
ssh_cmd = f"sshpass -p '{config.password}' ssh ... {config.username}@{config.host}"
test_cmd = f"{ssh_cmd} 'echo {config.sudo_password} | sudo -S echo OK'"
```

Если `password` содержит `'`, можно выполнить произвольную команду.

**Исправление:** Используйте `subprocess.run([...])` с массивом аргументов вместо `shell=True`:

```python
result = subprocess.run(
    ["sshpass", "-p", config.password, "ssh",
     "-o", "StrictHostKeyChecking=no",
     "-o", "UserKnownHostsFile=/dev/null",
     "-o", f"ConnectTimeout={timeout}",
     "-p", str(config.port),
     f"{config.username}@{config.host}",
     "echo OK"],
    capture_output=True, text=True, timeout=20
)
```

**1.3. Docker socket смонтирован в API** (`docker-compose.yml:63`)

```yaml
/var/run/docker.sock:/var/run/docker.sock
```

Это даёт API полный контроль над Docker хоста (root эквивалент). Контейнер `kag-api` добавлен в группу `docker` (`Dockerfile:56`).

**Исправление:** Убрать монтирование docker.sock. Если нужен мониторинг контейнеров — использовать отдельный прокси (например, Traefik) или Docker API через TLS с ограниченными правами.

**1.4. Auth отключена по умолчанию** (`auth.py:10`, `docker-compose.yml:30`)

`AUTH_ENABLED=false` — все эндпоинты открыты. Даже админские (`/api/v1/admin/*`, `/api/v1/admin/cache/clear`).

**Исправление:** Для production профиля установить `AUTH_ENABLED=true`. Добавить проверку профиля окружения.

**1.5. CORS полностью открыт** (`main.py:83-90`)

```python
if True or os.getenv("ENABLE_CORS", "false").lower() == "true":
    allow_origins=["*"]
```

Условие `if True` — CORS навсегда включён для всех origins.

**Исправление:**

```python
allowed_origins = os.getenv("CORS_ORIGINS", "").split(",")
if allowed_origins and allowed_origins[0]:
    app.add_middleware(
        CORSMMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
```

**1.6. Default пароли** (`docker-compose.yml:241-242`)

Keycloak admin: `admin/admin`. PostgreSQL: `keycloak/keycloak_password`. Grafana: `admin/admin`.

**Исправление:** Все дефолтные пароли должны быть обязательными переменными окружения без fallback-значений в production.

### Серьёзные проблемы

**1.7. Static token comparison** (`auth.py:73`)

```python
if STATIC_TOKEN and token == STATIC_TOKEN:
```

Прямое сравнение строк уязвимо к timing-атакам.

**Исправление:**

```python
import hmac
if STATIC_TOKEN and hmac.compare_digest(token, STATIC_TOKEN):
```

**1.8. No rate limiting на upload** (`upload.py:17`)

Нет ограничения на количество/размер файлов. `MAX_FILE_SIZE` в `validator.py` объявлен, но **не используется** в маршрутах upload.

**Исправление:** Добавить `slowapi` или кастомный middleware для rate limiting. Вызвать `SecurityValidator.validate_file_upload()` в эндпоинтах upload.

**1.9. No input validation на upload** (`upload.py:38-39`)

`SecurityValidator.validate_file_upload()` существует, но **не вызывается** ни в `upload_document`, ни в `upload_documents_batch`.

**Исправление:**

```python
from src.security.validator import SecurityValidator

content = await file.read()
SecurityValidator.validate_file_upload(
    file_path=save_path,
    filename=file.filename,
    file_size=len(content),
    mime_type=file.content_type
)
```

**1.10. Keycloak availability check на каждый запрос** (`auth.py:37-53`)

При включённой авторизации каждый HTTP-запрос делает синхронный HTTP-запрос к Keycloak. Это DoS-вектор и bottleneck.

**Исправление:** Кэшировать статус Keycloak (проверять раз в 30 секунд), использовать async httpx (`httpx.AsyncClient`).

**1.11. Encryption key в контейнере** (`gost_crypto.py:42-44`)

Ключ шифрования хранится в `/app/data/.encryption_key` внутри контейнера. При пересоздании контейнера ключ теряется. Нет интеграции с Vault/KMS.

**Исправление:** Для production — использовать Docker secrets или HashiCorp Vault. Как минимум — монтировать `/app/data` через named volume.

**1.12. `chmod 777`** (`Dockerfile:55`)

```dockerfile
chmod -R 777 /app/data
```

**Исправление:** `chmod -R 750 /app/data`, убедиться что пользователь `kag` является владельцем.

---

## 2. УДОБСТВО И КАЧЕСТВО КОДА — Оценка: 5/10

### Положительные стороны

- Хорошая структура проекта: разделение на `api/`, `indexing/`, `llm/`, `security/`, `agents/`
- Pydantic-модели для валидации
- Многоуровневый LLM-роутер с fallback (Ollama → vLLM → OpenAI)
- Мониторинг из коробки (Prometheus, OpenTelemetry, Grafana, Loki)
- Docker profiles (dev/prod/monitoring)
- Аудит логирование с SIEM-форматированием
- Health checks на всех сервисах
- Resource limits в Docker Compose

### Проблемы

**2.1. Дублирование Qdrant-клиентов**

- `qdrant_service.py` — REST API клиент
- `embeddings_service.py` — `qdrant-client` (SDK) + ещё и raw `requests` внутри

Используются оба, но `qdrant_service.py` почти не используется. Выбрать один подход и придерживаться его.

**Рекомендация:** Оставить `qdrant_service.py` как унифицированный слой доступа к Qdrant. Переписать `embeddings_service.py` чтобы использовал его вместо прямого доступа.

**2.2. Дублирование чанкеров**

- `parsers.py:TextChunker` (1000 символов, 200 overlap)
- `chunking.py:DocumentChunker` (500 символов, 50 overlap)

Оба существуют, оба используются.

**Рекомендация:** Оставить один чанкер с настраиваемыми параметрами (через config). Удалить второй.

**2.3. Много TODO/заглушек**

- `executor.py` — все обработчики возвращают заглушки
- `evaluator.py` — метрики качества: `return 0.7  # Заглушка`
- `admin.py` — все эндпоинты возвращают пустые данные
- `planner.py` — анализ запросов не реализован

**Рекомендация:** Либо реализовать, либо убрать из production-маршрутов. Заглушки маскируют нереализованный функционал.

**2.4. Глобальные синглтоны**

Почти каждый модуль создаёт глобальный экземпляр на уровне импорта:

```python
gost_crypto = GOSTCrypto()       # создаёт ключ шифрования при импорте
ssh_manager = SSHConnectionManager()
embeddings_service = EmbeddingsService()
chat_service = ChatService()
document_parser = DocumentParser()
```

Это затрудняет тестирование и вызывает side effects при импорте.

**Рекомендация:** Использовать FastAPI dependency injection:

```python
def get_chat_service() -> ChatService:
    return ChatService()

@router.post("/chat")
async def chat(
    request: ChatRequest,
    service: ChatService = Depends(get_chat_service)
):
    ...
```

**2.5. Смешанный sync/async**

`qdrant_service.py` — sync `httpx.Client`, `embeddings_service.py` — async методы + sync `requests` внутри async. Это может вызывать блокировку event loop.

**Рекомендация:** Заменить все sync HTTP-вызовы в async контексте на `httpx.AsyncClient` + `await`. Для sync кода (Celery worker) — использовать отдельные sync клиенты.

**2.6. Dead code в embeddings_service**

```python
    except Exception as e:
        logger.error(f"Ошибка удаления документа: {e}")
        return False

    except Exception as e:  # Unreachable!
```

**Рекомендация:** Удалить недостижимый блок.

**2.7. Версия API hardcoded**

`version="0.1.0"` в `main.py`, `Settings`, и других местах вместо чтения из `pyproject.toml`.

**Рекомендация:** Читать версию из `pyproject.toml`:

```python
from importlib.metadata import version
APP_VERSION = version("kag")
```

---

## 3. АРХИТЕКТУРА — Оценка: 6/10

### Что хорошо

- RAG pipeline: upload → parse → chunk → embed → store → search → generate
- LLM router с приоритетами и health checks
- Keycloak + Casbin для authz
- Docker Compose profiles для окружений

### Что нужно улучшить

**3.1. Нет миграций БД**

SQLAlchemy models определены (`database/models.py`), но нет Alembic или механизма создания таблиц.

**Рекомендация:** Добавить Alembic для миграций.

**3.2. Нет тестов**

Папка `tests/` пустая или с заглушками.

**Рекомендация:** Добавить хотя бы:
- Unit-тесты для `security/validator.py`
- Integration-тесты для API endpoints
- Тесты для LLM router (mock)

**3.3. CI/CD без шагов безопасности**

`.gitlab-ci.yml` не описывает шаги тестирования/безопасности.

**Рекомендация:** Добавить стадии:
- `lint` (ruff)
- `test` (pytest)
- `security` (bandit, safety)
- `build`

**3.4. Нет graceful shutdown для Celery worker**

Worker может прервать обработку документа.

**Рекомендация:** Настроить `worker_shutdown` signal handler.

---

## Итоговая таблица

| Критерий | Оценка | Комментарий |
|----------|--------|-------------|
| Безопасность | 3/10 | Shell injection, ключи в git, auth отключена, CORS открыт |
| Удобство/UX | 5/10 | Хороший фундамент, но много заглушек и дублирований |
| Архитектура | 6/10 | Правильное направление, нужен рефакторинг |
| Качество кода | 5/10 | Читаемый, но глобальные синглтоны и dead code |
| Готовность к production | 2/10 | Критические уязвимости, незавершённые модули |
| **Общая** | **5/10** | **Рабочий прототип, требует значительного усиления безопасности и доработки** |

---

## Приоритеты исправления

### P0 — Немедленно (блокирует production)

1. Убрать `api_key.txt` из git, перегенерировать ключи
2. Исправить shell injection в `ssh_manager.py` — использовать `subprocess.run()` с массивом без `shell=True`
3. Убрать монтирование Docker socket из docker-compose.yml
4. Исправить `if True` в CORS middleware

### P1 — Высокий (до production)

5. Включить авторизацию для production профиля
6. Добавить вызов `SecurityValidator.validate_file_upload()` в upload endpoints
7. Rate limiting на upload/chat endpoints
8. Заменить `==` на `hmac.compare_digest()` для token comparison
9. Кэшировать Keycloak availability check
10. Убрать `chmod 777` из Dockerfile

### P2 — Средний (следующий спринт)

11. Убрать дублирование Qdrant-клиентов — выбрать один подход
12. Убрать дублирование чанкеров
13. Заменить глобальные синглтоны на FastAPI Depends()
14. Исправить sync/async mixing в embeddings_service
15. Удалить dead code
16. Добавить базовые тесты

### P3 — Низкий (технический долг)

17. Добавить Alembic миграции
18. Читать версию из pyproject.toml
19. Добавить graceful shutdown для Celery
20. Доработать CI/CD pipeline (lint, test, security scan)
21. Реализовать или убрать TODO-заглушки
