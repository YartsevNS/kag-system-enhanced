# Узкие места и проблемы — KAG System

Формат: симптом → причина → решение → файлы/коммиты → как диагностировать снова.

---

## 1. Граф знаний не наполняется — LLM возвращает пустые ответы (АКТУАЛЬНО)

- **Симптом:** в логах worker `Пустой ответ LLM для chunk_00001 (pass=extract):` для ВСЕХ чанков; в Neo4j только узлы Document/Chunk, сущностей (Entity) нет; граф занимает ~70 сек на документ впустую.
- **Причина:** `function_map:graph` → `deepseek-v4-flash` (api.deepseek.com). Модель не возвращает JSON в формате промпта извлечения сущностей — пустые ответы на всех 10 чанках. Это тот же класс проблемы, что в чате: flash-модель молчит на больших промптах / требует определённого формата.
- **Решение:** (в работе) — разобрать промпт entity_extractor, проверить формат ответа; рассмотреть смену модели для graph (локальная Ollama или более сильная модель) или переписать промпт/парсинг.
- **Файлы:** `src/indexing/entity_extractor.py` (`_call_llm`, pass=extract), `function_map:graph`.
- **Диагностика:** `docker logs kag-worker | grep 'Пустой ответ LLM'`; `docker exec kag-neo4j cypher-shell -u neo4j -p <pass> 'MATCH (e:Entity) RETURN count(e)'`.
- **⚠️ Следствие:** 70 сек на документ тратятся впустую; переиндексация 150+ документов = ~3 часа из-за этого.

## 2. RecursiveCharacterTextSplitter игнорирует chunk_overlap

- **Симптом:** настройка overlap из админки «не работала» — в чанках Qdrant перекрытия нет.
- **Причина:** langchain применяет overlap только при посимвольной резке (separator=""); при разбиении по разделителям склеивает встык.
- **Решение:** ручной overlap после split_text (хвост prev_text[-overlap:] в начало следующего). Проверено 71/71 пар.
- **Файлы/коммиты:** `src/indexing/chunking.py` — 8ada2dd, 5a97bbb.

## 3. chunk_size > лимит эмбеддинга → часть текста не индексируется

- **Симптом:** при chunk_size=800 и лимите 500 символов хвост чанка (~300 символов) не попадал в вектор; поиск не находил информацию из этой части документа.
- **Причина:** `MAX_INPUT_CHARS = 500` в `src/llm/embeddings.py` (лимит GigaChat ~514 токенов; при превышении 413 — батч отклоняется ЦЕЛИКОМ).
- **Решение:** chunk_size=500, overlap 15%. Админка предупреждает; /embedding-guide объясняет.
- **Файлы/коммиты:** `src/indexing/chunking.py`, `src/api/static/admin.html` — 8ada2dd, f450c1f.

## 4. OOMKilled worker на сканах (Occular)

- **Симптом:** документ 7cdd4ad4 (PDF 9.1 МБ, 6 стр сканов) завис на 30% 26 мин; `OOMKilled: true, RestartCount=22`; бесконечный цикл OOM→recovery→OOM.
- **Причина:** Occular (трансформерный OCR) на hi-res сканах превышает лимит 4G/2CPU.
- **Решение:** лимиты 4 CPU/12G через админку (живой docker update + персистентный патч compose). После: completed 16 чанков, OOM: false.
- **Файлы/коммиты:** `src/api/routes/admin_models.py`, `admin.html`, compose — 2a16c92, 18223ab.
- **Диагностика:** `docker inspect kag-worker --format '{{.State.OOMKilled}}'`, `docker logs kag-worker | grep OOM`.

## 5. Дубли задач (22978) — QueueGuard

- **Симптом:** 22978 дублей в очереди; документы обрабатывались многократно.
- **Причина:** recovery Beat-тики + двойной старт worker; задача-дубль не отличала живой processing от мёртвого следа.
- **Решение:** QueueGuard (SET NX, TTL 6ч), уровень 3 в задаче, снятие замка при skipped, recovery снимает замок перед перезапуском, force пробрасывается.
- **Файлы/коммиты:** `src/indexing/queue_guard.py`, `tasks.py`, `recovery.py` — 4acfec4, 05fd05e, 9efb2a8.
- **Диагностика:** `docker exec kag-redis redis-cli -n 1 LLEN documents`, `KEYS 'qguard:*'`.

## 6. Документ висел на графе >60 мин

- **Симптом:** 10fce2f1 processing без прогресса; worker solo-пул замирал.
- **Причина:** синхронные Neo4j/LLM-операции блокировали event loop.
- **Решение:** to_thread + wait_for (20 сек/операция, 60 сек/чанк, 300 сек/весь граф); граф пропускается при превышении.
- **Файлы/коммиты:** `document_service.py`, `entity_extractor.py` — 9efb2a8.

## 7. flash-модель даёт пустые ответы в чате

- **Симптом:** пустой ответ при промпте >~3К токенов; max_tokens 150-400 → пусто, 500+ → ответ.
- **Причина:** ограничения deepseek-v4-flash.
- **Решение:** маршрутизация интента (count/list через SQL без RAG), список limit 25, count без списка; фронтенд шлёт 2048.
- **Файлы/коммиты:** `chat_service.py`, `chat.py` — d0c7ad7, 95c025e.

## 8. Утечка чатов между пользователями на одном компьютере

- **Симптом:** новый пользователь видел чужие чаты; localStorage не чистился при логине.
- **Причина:** localStorage хранил сессии без привязки к пользователю; cookie vs header авторизация.
- **Решение:** cookie приоритетнее header; чистка localStorage при логине; серверные сессии SQL.
- **Файлы/коммиты:** `src/api/middleware/auth_v2.py` — b4e4ab6, 9ee6e1a, 6f8da96.

## 9. event loop блокировался синхронным I/O в api

- **Симптом:** health_check бэкендов и docker/stats «вешали» api; DNS в httpx блокировал.
- **Причина:** синхронный docker SDK, subprocess, DNS в async-роутах.
- **Решение:** to_thread + wait_for.
- **Файлы/коммиты:** `src/api/routes/admin_models.py`, health — a0fc72f, 94ecc0b, 8d9822d.

## 10. Neo4j NODE KEY constraint падал в Community

- **Симптом:** `NODE KEY` / composite constraints недоступны в Neo4j Community — schema init падал.
- **Причина:** Enterprise-фича.
- **Решение:** MERGE + отдельные индексы (idx_chunk_id, уникальный constraint на Chunk.id).
- **Файлы/коммиты:** `src/indexing/knowledge_graph.py` — 9865f40.

## 11. deploy.sh писал литерал `***` в KAG_DB_URL

- **Симптом:** KAG_DB_URL=...:***@kag-db — пароль не подставлялся.
- **Причина:** шаблон с `***` вместо `${KAG_DB_PASSWORD}`.
- **Решение:** `KAG_DB_URL=postgresql://kag:${KAG_DB_PASSWORD}@kag-db:5432/kag`.
- **Файлы/коммиты:** `deploy.sh` — d82f1a5, 2e7d850.

## 12. create_all не добавлял колонки в существующую таблицу documents

- **Симптом:** после расширения модели (24 колонки) старые БД не получали новые колонки.
- **Причина:** алембики нет; create_all создаёт только новые таблицы.
- **Решение:** `ensure_schema()` — create_all + ALTER TABLE ADD COLUMN идемпотентно.
- **Файлы/коммиты:** `src/database/migrations.py` — 2e7d850.

## 13. document_analyzer падал с 401 (DeepSeek)

- **Симптом:** `LLM недоступен для анализа: 401`; слал `/api/generate` на DeepSeek API.
- **Причина:** endpoint не совместим с OpenAI-стилем (нужен /chat/completions).
- **Решение:** provider-совместимый endpoint; 401 = не тратит токены, но баг.
- **Файлы/коммиты:** `src/llm/*.py` — 9f1ccba.

## 14. `seen is not defined` в чате (ReferenceError)

- **Симптом:** «❌ Ошибка: seen is not defined» при пустых sources.
- **Причина:** `const seen = new Map()` объявлена внутри блока `if (sources...)`.
- **Решение:** объявить ДО блока.
- **Файлы/коммиты:** `src/api/static/chat.html` — 95c025e.

## 15. scp нескольких файлов перезаписывает их друг другом

- **Симптом:** при `scp file1 file2 file3 dest/` файлы оказываются в dest/ с перезаписью (последний побеждает) — потеря изменений.
- **Причина:** scp нескольких файлов в существующую директорию кладёт их туда с basename; при указании dest как корня они все ложатся в корень.
- **Решение:** копировать ПО ОДНОМУ с полным путём назначения: `scp src/a.py user@host:/path/to/a.py && scp src/b.py user@host:/path/to/b.py`.
- **Диагностика:** после массового scp проверять `ls -la` целевые файлы и grep в контейнере.

## 16. pkill по паттерну убивает свой ssh

- **Симптом:** `pkill -f 'monitor/check'` убивал ssh-сессию (паттерн в командной строке).
- **Причина:** ssh передаёт командную строку, pkill матчится на неё.
- **Решение:** kill по PID, найденному заранее.

## 17. Embedding-клиент None после рестарта api (RAG молча 0 источников)

- **Симптом:** чат отдавал 0 источников без ошибок после рестарта api.
- **Причина:** `_embedding_client` создавался только в `initialize()`; после рестарта был None.
- **Решение:** ленивая автоинициализация в `search()`.
- **Файлы/коммиты:** `src/indexing/embeddings_service.py` — 27a980a.
