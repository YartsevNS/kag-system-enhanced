# План подготовки KAG к выводу в прод (fresh deploy с GitHub)

Дата: 2026-08-29. Ветка-источник: stable_PyMuPDF (HEAD be12616).
Цель: после `git clone` с GitHub на пустой сервер проект должен развернуться,
подключить нейросети и работать без ручных правок.

## Текущее состояние (перепроверено)

- Три точки синхронны: локаль + GitHub + сервер 18 = be12616. Сервер git-дерево чистое.
- Ветки на origin: `main` (72896d9, 24.08), `stable` (63d66dd, 23.08),
  `stable_PyMuPDF` (be12616, 28.08 — самая свежая, отставание main −56, stable −91).
- Аудит (29.08) подтверждён: 7 критичных багов, ~6000 строк мёртвого кода, 11 групп дублей.

## Фаза 0 — ветка и чистка git (подготовка)

1. Создать ветку `preprod` от stable_PyMuPDF.
2. Удалить мусор из git (не в .gitignore, реально коммитится):
   - `user_data/uploads/*.docx` — тестовые литературные книги (чужие данные)
   - `user_data/api_token.json` — секрет
   - `session-ses_2402.md` (144KB), `TRACE_TEST.md`, `LAUNCH_STATUS.md`,
     `SESSIONS_SUMMARY.md`, `project.txt`, `TESTING.md`, `CHANGELOG.md`,
     `test_document.docx`, `check_docs.py`, `check_neo.py`
   - папка `1/` (Qwen-мусор), `.qwen/`
   - `docker-compose-clean.yml`, `docker-compose.pg.yml` (лишние копии)
   - `.gitlab-ci.yml` (проект на GitHub, не GitLab)
3. Дополнить .gitignore: `user_data/api_token.json`, `user_data/uploads/`, `.hermes/`.
4. `index.html` без роута и `know.html` (заглушка 23 строки) — решить: удалить или оставить.

## Фаза 1 — критичные баги кода (7, блокируют прод)

1. `/upload/bulk` (zip/tar) — `asyncio.run()` внутри async → RuntimeError.
   `src/api/routes/upload.py:705` (вызовы :637/:658). → `await`/to_thread.
2. `/admin/ad-config` → 500 NameError: `config_store` не импортирован.
   `src/api/routes/admin.py:191,223`.
3. `/auth/refresh` ставит cookie `secure=False` хардкодом — на HTTPS refresh сломан.
   `src/api/routes/auth.py:325` (в /login корректно через `_is_secure`).
4. Суммаризация мертва: `config_store` до импорта + `parsed_text` не определён.
   `src/api/services/document_service.py:565,570,592`.
5. `PUT /admin/models/worker-resources` и `PUT /system-config` патчат docker-compose.yml
   на хосте и перезапускают контейнеры — нарушение правил проекта (убивало сервер).
   `src/api/routes/admin_models.py:2364-2486, 2110-2234`. → сделать read-only или убрать.
6. `vectorize_document` вызывает `Vectorizer()` без импорта (NameError) — мёртвый код.
   `src/indexing/tasks.py:186` + `src/indexing/vectorizer.py` (заглушки). → удалить оба.
7. `GET /kg/validate/{document_id}` без admin-роли (только JWT). `knowledge_graph.py:250`.

## Фаза 2 — нейросети без хардкодов (подключение после деплоя)

Все адреса — из .env / админки, не в коде. Сейчас захардкожено (~20 мест):
- `config.py:79,106` — OLLAMA_BASE_URL / EMBEDDING_BASE_URL (ок, это дефолты env)
- `indexing/llm_ocr.py:42,279`, `indexing/ocr_engine.py:44` — 192.168.50.41:11434
- `api/services/model_manager.py:460-461`, `provider_service.py:19,40,41,470`
- `api/routes/admin_models.py:36,583,708`, `type_watchdog.py:204`, `llm/embeddings.py:38`
Целевые нейросети (должны задаваться одним местом — админка → provider/function_map):
- Ollama LLM: http://192.168.50.41:11434 (phi4-mini / qwen2.5)
- Embedding: http://192.168.50.42:8090/v1 (gigachat, 1024 dim)

## Фаза 3 — инфраструктура fresh deploy

1. **Dockerfile (api)** — убрать то, что api не нужно:
   - `torch torchvision` (3.5GB, нужен только worker; api-образ раздут)
   - `build-essential` (не нужен — все пакеты с wheel'ами)
   - `sshpass` — НЕ убирать (используется ssh_manager.py:169 и admin_models.py:464
     для «тест SSH-подключения»), но ЗАМЕНИТЬ на paramiko в отдельной фазе (пароль
     не светится в argv/ps). Не блокирует прод.
2. **Дубль запуска api: entrypoint.sh vs compose `command`** — сейчас в Dockerfile
   `ENTRYPOINT ["/entrypoint.sh"]`, а entrypoint.sh не читает `$@`, поэтому compose
   `command: sh -c "..."` молча игнорируется (работает только entrypoint.sh).
   Два механизма с разным содержимым (chmod 666 vs chmod 777) — устранить: оставить
   ОДИН способ запуска (compose command), убрать ENTRYPOINT из Dockerfile.
3. **chmod 666 docker.sock / chmod 777 data** — заменить на `group_add` с gid
   хостовой docker-группы (docker.sock остаётся 660 root:docker, kag в группе —
   доступ есть, посторонним нет). Для /app/data — `chown kag:kag` + 750 вместо 777.
4. **Dockerfile (api)** веса Occular качаются без retry и без fail-hard
   (`curl ... || echo WARNING`). При неудаче образ без весов. → retry + exit 1, как в worker.
   Либо убрать Occular из api вообще (OCR делает worker).
5. **docker-compose.yml** — monitoring (prometheus/grafana/loki/otel) запускается ВСЕГДА
   (нет profiles) вопреки правилам проекта. → вынести в `profiles: [monitoring]`.
6. **Рассинхрон конфигов** (единый источник истины — .env):
   - `QDRANT_HOST`: config.py нормализует `qdrant`→`kag-qdrant` (config.py:222), compose=qdrant
   - `KC_DB_HOST`: .env.example=`keycloak-db`, compose=`kag-db` (неверно в example)
   - `EMBEDDING_DIMENSIONS`: example=768, compose=1024, config=1024
   - `EMBEDDING_MODEL`: example=nomic-embed-text, compose=Embeddings
   - `EMBEDDING_BASE_URL`: example=host.docker.internal, compose=192.168.50.42:8090
   - `OLLAMA_BASE_URL`: example=host.docker.internal, compose=192.168.50.41
   - `OLLAMA_MODEL`: example=mistral:7b, compose=phi4-mini
7. **deploy.sh** — `.env` пишется с `KAG_DB_URL=postgresql://kag:***@...` (маска),
   реальный пароль появляется только после init-all → писать реальный пароль сразу.
   Плюс EMBEDDING/OLLAMA адреса захардкожены (192.168.50.41/42) — при fresh deploy
   на другом сервере менять вручную.

## Фаза 4 — мёртвый код и дубли (не блокирует прод, но чистит ~6000 строк)

- Удалить: `src/agents/*`, `src/evaluation/*`, `src/llm/{router,ollama_client,
  openai_client,vllm_client,base,models,exceptions}` (оставить embeddings.py),
  `vectorizer.py`, `scheduler.py` (APScheduler), `middleware/auth.py`, `auth_gate.py`.
- Слить: 3 затенённых дубля в admin_models.py; 2 генератора миниатюр в document_service;
  rebuild_watchdog vs rebuild_graph_task (оставить Celery).
- Выбрать один watcher: web_monitor (осн.) + hot_folder_watcher; удалить старые
  web_watcher/folder_watcher + роут watchers.py.

## Фаза 5 — тесты

- 34/167 тестов падают (устарели: ждут старый JSON на /, /chat без auth).
- Починить или пометить skip; сделать тесты не требующими внешних БД.

## Фаза 6 — приёмка fresh deploy (на сервере)

1. `git clone` в чистую папку → `bash deploy.sh` → compose up.
2. `/setup` → Initialize ALL (генерирует пароли, БД, admin).
3. Админка → подключить провайдеры (Ollama 41, embedding 42).
4. Smoke: health, login, загрузка PDF → обработка → Qdrant/Neo4j, чат-RAG.
5. Проверить HTTPS (refresh-токен, cookie secure), SSO Keycloak.

## Порядок выполнения (приоритет)

Блокирует прод: Фаза 1 (баги) → Фаза 2 (нейросети) → Фаза 3 (инфраструктура) → Фаза 6 (приёмка).
Опционально/после: Фаза 4 (чистка), Фаза 5 (тесты).
