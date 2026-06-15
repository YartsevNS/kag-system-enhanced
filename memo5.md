# KAG Project - Отчёт: Исправления, Qdrant мониторинг и страница чанков (25 апреля 2026)

## Выполненные исправления

### 1. Проблема с обработкой документов

**Симптом:** Документы загружались, но зависали на 50% (векторизация).

**Причина:** 
- Ollama была недоступна по старому адресу `host.docker.internal:11434`
- Qdrant коллекция имела неправильную размерность (4096 вместо 768)
- В коде не было обработки таблиц из DOCX файлов

**Решение:**
- Исправлен URL Ollama: `192.168.50.41:11434` (локальный компьютер)
- Пересоздана Qdrant коллекция с размерностью 768
- Улучшен DOCX парсер - теперь извлекает таблицы

### 2. Qdrant мониторинг

Создан новый сервис для мониторинга векторной базы данных:
- `src/api/services/qdrant_monitor.py`
- API endpoints:
  - `/api/v1/admin/models/qdrant/info` - полная информация
  - `/api/v1/admin/models/qdrant/collections` - список коллекций
  - `/api/v1/admin/models/qdrant/collections/{name}/chunks` - чанки
- Web страница: `/qdrant`

### 3. Страница чанков

- `/chunks` - просмотр всех чанков из Qdrant
- Карточки с превью содержимого
- Клик для просмотра полного текста в модальном окне
- Поиск по содержимому и ID
- Отображение метаданных

### 4. Навигация

Добавлены кнопки на главную страницу (index.html):
- 📚 Чанки
- 🗄️ Qdrant
- 📄 Документы
- 🐳 Docker

Кнопки также на страницах documents.html, qdrant.html, chunks.html.

## Технические детали

### Конфигурация Ollama (локальный компьютер)
```
OLLAMA_BASE_URL: http://192.168.50.41:11434
EMBEDDING_BASE_URL: http://192.168.50.41:11434
```

### Доступные модели на Ollama
- nomic-embed-text:latest (для embeddings)
- phi4-mini:latest (для LLM)
- qwen3.6:27b, qwen3.6:35b
- mistral:7b
- И многие другие

### Qdrant коллекция
- Имя: `kag_documents`
- Размерность: 768
- Distance: COSINE

## Использованные проекты

### paperless-gpt (https://github.com/icereed/paperless-gpt)
Идеи по обработке документов:
- OCR via Tesseract для PDF
- LLM-based OCR для сложных случаев
- Многоэтапная обработка документов

### paperless-ai (https://github.com/clusterzx/paperless-ai)
RAG-based документный поиск

### KAG от OpenSPG (https://github.com/OpenSPG/KAG)
Интересный проект, но имеет проприетарный UI (Docker на порту 8887). 
Не стали копировать - сделали свой открытый аналог.

## Текущий статус системы

### Контейнеры
| Контейнер | Статус |
|-----------|--------|
| kag-api | Up (healthy) |
| kag-mcp | Up (healthy) |
| kag-worker | Up |
| kag-scheduler | Up |
| kag-qdrant | Up |
| kag-keycloak | Up (unhealthy) |
| kag-keycloak-db | Up (healthy) |
| kag-redis | Up (healthy) |

### Доступные страницы
- http://localhost:8000/ - Чат
- http://localhost:8000/documents - Загрузка документов
- http://localhost:8000/chunks - Просмотр чанков
- http://localhost:8000/qdrant - Qdrant мониторинг
- http://localhost:8000/docker - Docker мониторинг
- http://localhost:8000/admin - Настройки

## GitHub коммиты

```
2b2aa80 feat: Улучшенная навигация и страница чанков
87c84e9 feat: Добавлена страница чанков + исправления
d1d9b74 fix: Исправлены URL подключения к Ollama
339506a fix: Улучшенный DOCX парсер + добавлен Ollama контейнер
16a288a feat: Qdrant мониторинг с web dashboard
41f7faf fix: Исправления ошибок и улучшения мониторинга
```

## Как загрузить документ

1. Открыть http://localhost:8000/documents
2. Загрузить файл (PDF, DOCX, TXT, CSV)
3. Документ автоматически парсится, разбивается на чанки, векторизуется
4. Проверить статус на странице или в /chunks

## Следующие шаги

- Добавить больше типов документов
- Улучшить качество OCR (см. paperless-gpt)
- Добавить поиск по чанкам с семантическим поиском
- Интеграция с LLM для генерации ответов

---

https://github.com/YartsevNS/kag-system