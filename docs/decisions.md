# Решения (ADR) — KAG System

Хронологический реестр ключевых решений: контекст → решение → почему → последствия → файлы/коммиты.

---

## [2026-08-23] Ручной overlap после RecursiveCharacterTextSplitter

- **Контекст:** настройка overlap из админки (200) «не работала» — в чанках Qdrant перекрытия не было.
- **Решение:** в `chunk_segments` после `split_text` добавлять хвост предыдущего СФОРМИРОВАННОГО чанка (`prev_text[-overlap:]`) в начало следующего; верхняя граница `chunk_size + overlap`; метка `overlap_applied` в metadata.
- **Почему:** RecursiveCharacterTextSplitter применяет chunk_overlap ТОЛЬКО при посимвольной резке (separator=""); при разбиении по разделителям (`\n\n`, `\n`, `. `) склеивает фрагменты встык без перекрытия — известное поведение langchain. Хвост берём из prev_text, а не split_texts[i-1], иначе граница «съезжает» и появляются дыры.
- **Последствия:** overlap в 71/71 парах соседних чанков (проверено на gost-r-34.pdf). Чанки 260-567 символов.
- **Файлы/коммиты:** `src/indexing/chunking.py` — 8ada2dd (первая версия), 5a97bbb (корректная версия).

## [2026-08-23] Очистка Qdrant/Neo4j при force-переобработке

- **Контекст:** при переиндексации с изменением числа чанков старые точки Qdrant оставались («осиротевшие»: 580 точек вместо 72 у gost-r-34.pdf).
- **Решение:** `document_service.process_document(document_id, force=True)` — сначала `embeddings_service.initialize()` + `delete_document(document_id)` (проверка результата!), затем `kg_service.clear_document(document_id)`, потом обычная обработка.
- **Почему:** `delete_document` при `_qdrant_client=None` молча возвращает False (ловит AttributeError внутри) — без явной инициализации старые точки оставались. Проверка результата даёт warning при сбое.
- **Последствия:** после force-переобработки в Qdrant ровно столько точек, сколько чанков (72/72).
- **Файлы/коммиты:** `src/indexing/tasks.py`, `src/api/services/document_service.py` — 5a97bbb.

## [2026-08-23] chunk_size 500 вместо 800; overlap в процентах в админке

- **Контекст:** при chunk_size=800 и лимите эмбеддинга 500 символов хвост чанка (~300 символов) НЕ попадал в вектор — часть текста не индексировалась.
- **Решение:** chunk_size=500 (ровно под лимит), overlap 15% (75 символов). В админке поле «Перекрытие (%)» (10-20%, дефолт 15%), в символы конвертируется автоматически.
- **Почему:** лимит GigaChat ~512 токенов ≈ 500 символов кириллицы; chunk_size больше лимита бессмыслен — хвост не векторизуется.
- **Последствия:** все новые документы индексируются полностью; старые (158) требуют переиндексации.
- **Файлы/коммиты:** `src/api/static/admin.html`, `src/llm/embeddings.py` (MAX_INPUT_CHARS=500), 8ada2dd, f450c1f.

## [2026-08-22] Soft-пауза обработки (дорогая фаза)

- **Контекст:** пользователь просил «мягко приостановить обработку, входим в дорогую фазу, продолжим после 13:10».
- **Решение:** `docker stop kag-worker` (текущая задача завершается, новые не берёт), остановка фоновой ЦБ-проверки (kill PID), очередь сохраняется в Redis.
- **Почему:** не терять очередь и состояние; scheduler может ставить задачи, но worker их не берёт → деньги не тратятся.
- **Последствия:** возобновление = `docker start kag-worker` + повторный запуск проверки ЦБ.

## [2026-08-21] Ресурсы worker из админки (4 CPU / 12G)

- **Контекст:** документ 7cdd4ad4 (сканы 9.1 МБ) упал в OOMKilled при лимите 4G/2CPU — Occular на hi-res сканах превышает 4GB, цикл OOM→recovery→OOM (RestartCount=22).
- **Решение:** админ-форма Worker Resources: живой docker update (прямой API `NanoCpus` + `Memory` + `MemorySwap` — обязателен, иначе 409) + персистентный патч docker-compose.yml + рестарт worker. Лимиты в compose через переменные `${WORKER_CPUS:-4.0}` / `${WORKER_MEMORY:-12G}`.
- **Почему:** docker SDK `update_container()` не поддерживает cpus/memory/NanoCpus; compose у worker read-only (rw=false), поэтому патч через одноразовый контейнер с `--entrypoint python3`.
- **Последствия:** 7cdd4ad4 обработан: completed, 16 чанков, OOM: false.
- **Файлы/коммиты:** `src/api/routes/admin_models.py`, `src/api/static/admin.html`, `docker-compose.yml` — 2a16c92, 18223ab.

## [2026-08-20] Серверное хранение сессий чата (SQL)

- **Контекст:** гонка вкладок localStorage теряла/перезаписывала диалоги; `'session_' + Date.now()` коллизии; мультиюзерность.
- **Решение:** `chat_sessions`/`chat_messages` (FK users, cascade, индексы) + `chat_storage.py` (CRUD с проверкой владельца) + эндпоинты `/chat/sessions*`; для авторизованных история для LLM из БД (источник истины); анонимы — localStorage fallback; 401 для анонимов by design.
- **Почему:** серверное хранение устраняет гонку вкладок и изолирует пользователей.
- **Последствия:** тесты с реальным admin прошли; изоляция пользователей работает.
- **Файлы/коммиты:** `src/database/chat_models.py`, `src/api/services/chat_storage.py`, `src/api/routes/chat.py`, `chat.html` — 6f8da96.

## [2026-08-20] Маршрутизация интента в чате (мета-запросы без RAG)

- **Контекст:** flash-модель даёт пустые ответы на больших промптах (>~3К токенов) и при max_tokens<500; список 60 документов ≈ 3400 токенов → пусто.
- **Решение:** `_detect_meta_intent(query)` — count/list/semantic по ключевым словам; list — SQL через document_repository (limit 25), count — без списка; `intent` в metadata ответа.
- **Почему:** мета-запросы («сколько документов», «перечисли») не требуют RAG — SQL быстрее и не упирается в лимиты flash.
- **Последствия:** «145 документов» отвечает корректно; семантика уходит в RAG.
- **Файлы/коммиты:** `src/api/services/chat_service.py`, `src/api/routes/chat.py` — d0c7ad7.

## [2026-08-20] Страховка графа знаний (таймауты)

- **Контекст:** документ 10fce2f1 висел на графе >60 мин; Neo4j/LLM блокировали event loop (worker solo-пул замирал целиком).
- **Решение:** Neo4j-операции через `asyncio.to_thread` + `wait_for` (20 сек), LLM-извлечение на чанк 60 сек, весь граф 300 сек — при превышении граф пропускается, документ завершается.
- **Почему:** граф вторичен, не должен блокировать завершение документа.
- **Файлы/коммиты:** `src/api/services/document_service.py`, `src/indexing/entity_extractor.py` — 9efb2a8.

## [2026-08-20] QueueGuard — защита от дублей

- **Контекст:** 22978 дублей задач в очереди; recovery Beat-тики + двойной старт создавали копии; документы обрабатывались многократно.
- **Решение:** единая точка постановки `enqueue_document` (SET NX, TTL 6ч > task_time_limit 2ч); уровень 3 в задаче: processing/completed без force → пропуск; при skipped — замок снимается сразу; recovery снимает замок перед перезапуском зависшего; force=True пробрасывается в `process_document.delay`.
- **Почему:** TTL 6ч > task_time_limit 2ч — замок гарантированно живёт дольше задачи.
- **Последствия:** 142/142 completed, очередь 0, замков 0.
- **Файлы/коммиты:** `src/indexing/queue_guard.py`, `src/indexing/tasks.py`, `src/indexing/recovery.py` — 4acfec4, 05fd05e, 9efb2a8.

## [2026-08-19] Эвристическая типизация без LLM

- **Контекст:** тип документа определялся отдельным LLM-процессом (дорого, медленно) или на лету без сохранения.
- **Решение:** auto_tagger regex-правилами сразу после парсинга (бесплатно): ГОСТ/СТО БР → standard, приказы/№ ОД- → order, методички/положения/регламенты → policy, инструкции/ТЗ → technical, сертификаты/пресс-релизы. Результат в БД + Qdrant payload. LLM type_watchdog только для сложных случаев по кнопке (батч 5).
- **Почему:** для нормативных документов правила покрывают ~90% случаев; LLM-типизация дорогая и медленная.
- **Файлы/коммиты:** `src/api/services/document_service.py`, auto_tagger — a6792d6, 13d1537.

## [2026-08-18] Переключение embedding на OpenAI-совместимый провайдер (GigaChat)

- **Контекст:** локальная Ollama (bge-m3 1024) на CPU — ~1 документ/мин, 128 документов gost.ru в очереди; пользователь видел расход 3 млн токенов DeepSeek/час.
- **Решение:** embedding через OpenAI-совместимый `/v1/embeddings` (GigaChat), батч 8, timeout 300с; `_ensure_collection` пересоздаёт коллекцию при смене размерности.
- **Почему:** bge-m3 на CPU медленный; GigaChat быстрее и качественнее для русского.
- **Последствия:** поиск score 0.5-0.58 вместо 0.3; размерность 1024.
- **Файлы/коммиты:** `src/llm/embeddings.py`, `src/indexing/embeddings_service.py` — 33bb6eb, ebcc8dc, b5e160a.

## [2026-08-16] Консолидация хранилища документов → SQL DocumentRepository

- **Контекст:** после git-отката проекта (`kag-rusystem-allv2` потеряла entrypoint.sh, document_repository.py и др.) восстановлена ветка main; три независимых engine (session.py, config_store, document_repository), config_store падал на keycloak-БД при пустом KAG_DB_URL.
- **Решение:** документы только в SQL (DocumentRepository, 24 колонки), config_store только настройки; единый engine + `ensure_schema()` (create_all + ALTER TABLE ADD COLUMN, алембики нет); deploy.sh пишет реальный пароль в KAG_DB_URL (был литерал `***`); setup.py сбрасывает все engine'ы.
- **Почему:** единый источник истины; create_all не добавляет колонки в существующие таблицы.
- **Файлы/коммиты:** `src/database/migrations.py`, `src/database/session.py`, `src/api/services/config_store.py`, `src/api/services/document_repository.py`, `deploy.sh`, `setup.py` — 2e7d850, d82f1a5, 05496b0, e38b2a6.

## [2026-08-16] Консолидация LLM → provider_service (function_map)

- **Контекст:** пять параллельных путей вызова LLM; хардкоды phi4-mini.
- **Решение:** provider_service — единый источник; `get_function_llm_config()`; function_map: chat/embedding/graph/doc_analysis → провайдер + модель; model_manager только CRUD моделей Ollama; fallback на legacy-настройки сохранён.
- **Почему:** один источник конфигурации, модели из админки, без хардкодов.
- **Файлы/коммиты:** `src/api/services/provider_service.py` — e38b2a6, 9f1ccba.
