# Архитектура KAG System (актуальная)

> Дата фиксации: 2026-08-23. Актуальность настроек проверять в админке (они меняются).
> Полная историческая версия: [ARCHITECTURE-LEGACY.md](ARCHITECTURE-LEGACY.md)

## 1. Сервисы (docker-compose.yml)

| Сервис | Роль | Порт |
|---|---|---|
| `kag-api` | FastAPI, REST API + статика (15+ HTML-страниц) | 8000 (только 127.0.0.1) |
| `kag-worker` | Celery worker (solo-пул), обработка документов | — |
| `kag-scheduler` | APScheduler — расписание веб-монитора | — |
| `kag-mcp` | MCP-сервер | 8001 |
| `kag-qdrant` | Векторная БД | 6333/6334 |
| `kag-redis` | Кэш + Celery broker (**db=1** для очереди) | 6379 |
| `kag-kag-db` | PostgreSQL 16 (kag + keycloak) | 5432 |
| `kag-neo4j` | Граф знаний | 7474/7687 |
| `kag-keycloak` | IdP (SSO) | 8080 (только 127.0.0.1) |
| `kag-nginx` | Единственная точка входа | 80/443 |
| `kag-otel-collector`, `kag-loki`, `kag-grafana`, `kag-prometheus` | Мониторинг | — |

**Внешний доступ:** nginx :80/:443 → api. api:8000 и keycloak:8080 только на 127.0.0.1.

**Worker лимиты:** 4 CPU / 12G (переменные `${WORKER_CPUS:-4.0}` / `${WORKER_MEMORY:-12G}` в compose; менять в админке «Ресурсы Worker» — живой docker update + персистентный патч compose).

## 2. Поток обработки документа

```
Загрузка (upload / веб-монитор)
  → дедупликация по хешу (SQL DocumentRepository)
  → QueueGuard: qguard:{doc_id} SET NX, TTL 6ч → очередь Celery db=1
  → process_document (worker, solo-пул, 1 за раз)
      1. PARSE (30%)  — PyMuPDF (текст. слой + find_tables) → Occular (сканы) → fallback DocumentParser
      2. Типизация    — auto_tagger эвристикой (regex, без LLM): standard/policy/order/technical/certificate/news
      3. CHUNKING (50%) — 500 символов, overlap 75 (ручной), RecursiveCharacterTextSplitter
      4. VECTORIZE (90%) — embedding GigaChat 1024 → Qdrant (batch 8, timeout 300)
      5. ANALYZE — LLM по первому чанку: title/type/summary (опционально, может падать)
      6. GRAPH — Neo4j: Document → HAS_CHUNK → Chunk → MENTIONS → Entity (LLM-извлечение, 10 чанков, таймауты)
  → completed (100%)
```

**force=True (переиндексация):** перед обработкой удаляются старые векторы Qdrant (delete_document) + очищается граф Neo4j (clear_document) — чтобы не оставались осиротевшие точки при изменении числа чанков.

## 3. Базы данных

### PostgreSQL (kag-kag-db)
- Таблицы: `documents` (24 колонки), `users`, `chat_sessions`, `chat_messages`, `system_configs` (config_store)
- **config_store** = только настройки (web_monitor sources/state/history, chunking, function_map, providers)
- **DocumentRepository** = документы (SQL — источник истины)
- Схема: `ensure_schema()` в `src/database/migrations.py` (create_all + ALTER TABLE ADD COLUMN)

### Qdrant (kag-qdrant)
- Коллекция `kag_documents`, dense 1024, COSINE (sparse есть в схеме, отключён для скорости)
- Payload: document_id, chunk_id, content, filename, file_type, group_ids, metadata (chunk_seq, overlap_applied)
- При смене размерности модели коллекция **пересоздаётся автоматически** (`_ensure_collection`)
- Индексы: document_id, chunk_id, file_type, filename, group_ids (KEYWORD)

### Neo4j (kag-neo4j, Community)
- Узлы: Document {id, filename}, Chunk {id, chunk_seq, text_preview}, Entity {name, type, source_docs}
- Связи: `(:Document)-[:HAS_CHUNK]->(:Chunk)`, `(:Chunk)-[:MENTIONS]->(:Entity)`, `(:Entity)-[:RELATED_TO]->(:Entity)`
- Community: NODE KEY / composite constraints НЕ доступны → MERGE + отдельные индексы
- Векторов в графе НЕТ — только текст-превью (первые ~500 символов)

### Redis (kag-redis)
- db=0: кэш, SSH-менеджер
- db=1: Celery broker (список `documents`), QueueGuard замки `qguard:{doc_id}`

## 4. Ключевые модули

| Файл | Назначение |
|---|---|
| `src/api/services/document_service.py` | Оркестрация обработки: parse → chunk → vectorize → analyze → graph. `process_document(document_id, force=False)` |
| `src/indexing/tasks.py` | Celery-задачи: `process_document`, `run_monitor_check`, `check_stuck_documents`. QueueGuard уровень 3 |
| `src/indexing/queue_guard.py` | Единая точка постановки задач, SET NX, TTL 6ч |
| `src/indexing/chunking.py` | DocumentChunker: 500/75, ручной overlap после split_text |
| `src/indexing/parsers.py` | DocumentParser + TextChunker (делегирует DocumentChunker) |
| `src/indexing/hybrid_parser.py` | PyMuPDF-first → Occular-only маршрутизация |
| `src/indexing/embeddings_service.py` | EmbedAndStore, _ensure_collection, delete_document, автопересоздание коллекции |
| `src/llm/embeddings.py` | EmbeddingClient: OpenAI-совместимый / Ollama, MAX_INPUT_CHARS=500 |
| `src/indexing/entity_extractor.py` | LLM-извлечение сущностей для графа (core/relations/extended → сейчас 1 промпт) |
| `src/indexing/knowledge_graph.py` | Neo4j: MERGE-операции, clear_document, гибридный поиск |
| `src/indexing/recovery.py` | Сброс зависших >60 мин, снятие замка перед перезапуском |
| `src/api/services/provider_service.py` | function_map: chat/embedding/graph/doc_analysis → провайдер + модель |
| `src/api/services/config_store.py` | PostgresConfigStore (PostgreSQL) |
| `src/api/routes/web_monitor.py` | Источники, run_check, история, BUILTIN_SOURCES |
| `src/api/routes/admin_models.py` | Настройки чанкинга, ресурсы worker, провайдеры |

## 5. Модели (function_map, актуально на 2026-08-23)

| Функция | Модель | Провайдер | Примечание |
|---|---|---|---|
| chat | deepseek-v4-flash | api.deepseek.com | ⚠️ пустой ответ при промпте >~3К токенов или max_tokens <500 |
| embedding | Embeddings (GigaChat) | /v1/embeddings, 1024 dim | лимит 500 символов, иначе 413 |
| graph | deepseek-v4-flash | api.deepseek.com | ⚠️ ВОЗВРАЩАЕТ ПУСТЫЕ ОТВЕТЫ (pass=extract) — граф фактически не наполняется |
| doc_analysis | deepseek-v4-flash | api.deepseek.com | эвристика auto_tagger работает без LLM |

## 6. Веб-монитор

- Источники в config_store (`web_monitor/sources`), история `web_monitor/history`
- Типы: rss, scrape, browser (Playwright — не установлен, НЕ работает), change
- BUILTIN_SOURCES: ЦБ РФ (акты по ИБ: `cbr.ru/information_security/acts/`, pagination `/Crosscut/LawActs/Page/95016?Page={page}`, лимит ~4-5 файлов/мин), ФСТЭК, ФСБ, ГОСТ Р, cit.cap.ru
- `_download_and_upload` — скачивание + upload через document_service (импорт ВНУТРИ try!)
- ЦБ-источник: ~80 документов, лимит item_delay=15с, batch_size=3

## 7. Фронтенд

- 21 страница, единая Supabase-тема (#171717, зелёный #3ecf8e, pill-кнопки)
- Без 🤖/🎯 (нейтральные ✨), ссылки на документы светлые
- Логи на страницах: новые строки СВЕРХУ
- `/embedding-guide` — гид по выбору модели (из админки)
- Кэш nginx: /static/ public 3600, /api/ no-store, thumbnail отдельно
