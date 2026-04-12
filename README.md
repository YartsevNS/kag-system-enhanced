# 🎯 KAG - Knowledge Augmentation Generation

> **Интеллектуальная система управления знаниями с RAG (Retrieval-Augmented Generation)**

KAG - это мощная система для работы с документами и знаниями, которая использует AI для поиска, обработки и генерации ответов на основе вашей базы знаний.

## 🚀 Быстрый старт

### 1. Развертывание через Docker

```bash
# Запуск всех сервисов
docker compose --profile dev up -d

# Проверка статуса
docker compose ps
```

### 2. Первоначальная настройка

После первого запуска откройте в браузере:

```
http://localhost:8000/setup
```

Вас встретит **Setup Wizard**, где вы сможете настроить:
- ✅ Подключение к базе данных PostgreSQL
- ✅ LLM бэкенд (Ollama или vLLM)
- ✅ Embedding модель
- ✅ SSH доступ для управления сервером

### 3. Начало работы

После настройки вы попадете на главную страницу чата:

```
http://localhost:8000
```

## 📦 Возможности

### 🤖 Чат с AI
- Интеллектуальный помощник с доступом к вашей базе знаний
- Контекстные ответы на основе загруженных документов
- Экспорт диалогов в PDF и DOCX

### 📄 Загрузка документов
- Поддержка PDF, DOCX, TXT, CSV
- Автоматический парсинг и векторизация
- Отслеживание статуса обработки

### 🔍 RAG Поиск
- Семантический поиск по базе знаний
- Интеграция с Qdrant для векторного поиска
- Настраиваемые параметры чанкинга

### ⚙️ Администрирование
- Управление моделями Ollama/vLLM
- Мониторинг Docker контейнеров
- Настройки SSH подключения
- Экспорт и импорт настроек

## 🛠️ Технологический стек

| Компонент | Технология |
|-----------|------------|
| **Backend** | Python 3.11+, FastAPI |
| **База данных** | PostgreSQL (Keycloak DB) |
| **Векторная БД** | Qdrant |
| **Кэш/Очереди** | Redis |
| **LLM** | Ollama / vLLM |
| **Embeddings** | Ollama Embeddings |
| **Docker** | Docker Compose |
| **Мониторинг** | Prometheus + Grafana |

## 📊 Архитектура

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Nginx     │────▶│  FastAPI     │────▶│   Qdrant    │
│  (Proxy)    │     │  (Backend)   │     │  (Vector)   │
└─────────────┘     └──────┬───────┘     └─────────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │   PostgreSQL │
                    │   (Configs)  │
                    └──────────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │    Redis     │
                    │   (Cache)    │
                    └──────────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │    Ollama    │
                    │  (LLM/Embed) │
                    └──────────────┘
```

## ⚙️ Настройка через Setup Wizard

При первом запуске вы увидите страницу настройки:

### 1. База данных
- **Хост**: Имя контейнера или IP
- **Порт**: 5432 (по умолчанию)
- **Имя базы**: keycloak
- **Пользователь**: keycloak
- **Пароль**: Ваш пароль

### 2. LLM Бэкенд
- **Тип**: Ollama или vLLM
- **Адрес**: IP сервера (например, 192.168.50.41)
- **Порт**: 11434 (Ollama) или 8000 (vLLM)
- **Модель**: Название модели (например, phi4-mini:latest)

### 3. Embedding модель
- **Модель**: qwen3-embedding:4b
- **Размерность**: 4096

### 4. SSH доступ
- **Пользователь**: nick
- **Пароль**: Ваш SSH пароль
- **Sudo пароль**: Если отличается

## 🔐 Безопасность

- ✅ Все пароли шифруются алгоритмом GOST
- ✅ Хранение в PostgreSQL с транзакционной целостностью
- ✅ SSH ключи и пароли защищены
- ✅ RBAC контроль доступа через Keycloak + Casbin

## 📁 Структура проекта

```
kag/
├── docker-compose.yml          # Docker конфигурация
├── Dockerfile                  # Образ API
├── src/
│   ├── api/                    # FastAPI приложение
│   │   ├── routes/             # API endpoints
│   │   ├── services/           # Бизнес-логика
│   │   ├── middleware/         # Middleware
│   │   └── static/             # Веб-интерфейс
│   ├── llm/                    # LLM клиенты
│   ├── indexing/               # Обработка документов
│   ├── auth/                   # Аутентификация
│   └── database/               # Модели БД
├── tests/                      # Тесты
└── docs/                       # Документация
```

## 🧪 Тестирование

```bash
# Запуск тестов
pytest tests/ -v

# Проверка синтаксиса
ruff check src/
```

## 🚀 Деплой в production

### 1. Подготовка

```bash
# Клонировать репозиторий
git clone https://github.com/your-org/kag.git
cd kag

# Настроить .env
cp .env.example .env
nano .env
```

### 2. Запуск

```bash
# Production профиль
docker compose --profile prod up -d

# С мониторингом
docker compose --profile prod --profile monitoring up -d
```

### 3. Проверка

```bash
# Health check
curl http://localhost:8000/api/v1/health

# Открыть Setup Wizard
open http://localhost:8000/setup
```

## 📈 Мониторинг

```bash
# Grafana дашборды
open http://localhost:3000

# Docker Dashboard
open http://localhost:8000/docker
```

## 🤝 Участие в проекте

1. Fork репозиторий
2. Создайте ветку (`git checkout -b feature/amazing-feature`)
3. Commit изменения (`git commit -m 'Add amazing feature'`)
4. Push (`git push origin feature/amazing-feature`)
5. Создайте Pull Request

## 📄 Лицензия

MIT License - см. файл [LICENSE](LICENSE) для деталей.

## 📞 Поддержка

- 📧 Email: support@kag-system.com
- 💬 Telegram: @kag_support
- 🐛 Issues: [GitHub Issues](https://github.com/your-org/kag/issues)

---

**Сделано с ❤️ командой KAG**
