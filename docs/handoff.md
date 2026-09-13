# Состояние работы — KAG (обновлено 2026-09-13, сессия по синхронному I/O в async)

Файл для возобновления после сжатия контекста или в новой сессии.

**Важно про расположение.** Раньше этот файл лежал в `.hermes/handoff.md` и больше не
обновляется: Hermes блокирует запись в файлы внутри project-local `.hermes/`
(жёсткое правило стража «protected instruction files», запись требует интерактивного
подтверждения, которое в CLI-сессии не приходит). Файл `.hermes/handoff.md` остался
**устаревшим** — не верить ему. Актуальное состояние: этот файл + `docs/guides/sync-io-audit.md`
+ память Mnemosyne (`project:kag/*`, `task:progress/*`).

## Где мы сейчас

- Репозиторий (ноутбук): `C:\VSCODE_PROJECT\kag-system-enhanced`, ветка **PREPROD**,
  последний коммит **0cd9950**, дерево чистое.
- Сервер 18 (`yartsevn@192.168.50.18`, `/home/yartsevn/kag-system`): `kag-api`,
  `kag-system_worker_1`, `worker-maintenance` на образе **2026.09.13.42**.
- Загрузка новых документов на стенде заблокирована галочкой (`UPLOADS_BLOCKED`);
  документов 47, все `completed`.
- Тесты: `test_warm_init_invalidation` (5), `test_config_store_cache` (5),
  `test_type_watchdog_registration` (15), `test_config_store_roundtrip`,
  `test_processing_block_guard` (10), `test_kg_route_guards`, `test_document_access`,
  `test_upload_route_guards`, `test_api_process_io`, `test_domain_schema_persistence` (17),
  `test_admin_audit_fixes`, `test_no_secrets_in_repo`, `test_chunk_order`.

## Главное этой сессии: карта синхронного I/O в async

Карта: **258 → 166 мест (145 уникальных)**. Инструмент — `scripts/scan_sync_io.py`
(в репозитории; сопоставление по получателю, иначе `app.get`/`status.get` дают тысячи
ложных). Обоснование остатка — `docs/guides/sync-io-audit.md`.

Закрыто (всё подтверждено замером на стенде):

| Файл | Что | Замер до → после |
|---|---|---|
| `embeddings_service.py` | 23 вызова синхронного Qdrant-клиента в 12 async-методах, включая `search()` — путь каждого запроса чата | чат 5 с в фоне → `/health` 1.4–2.4 мс |
| `embeddings_service.py` | `initialize()` звал пробный эмбеддинг на каждый запрос | 163 мс каждый раз → 971 мс холодная / 0.0 мс тёплая |
| `embeddings_service.py` | окно переинициализации 60 с при смене модели | подпись модели: смена → переинициализация сразу |
| `type_watchdog.py` | ФС/БД сторожа в его цикле → в поток | `/health` 2.2–3.1 мс во время работы сторожа |
| `routes/admin.py` | Keycloak (`urllib`) и `subprocess.check_output` в async → в поток | `/admin/keycloak/users` 0.387 с, `/health` 2.2–3.1 мс |
| `config_store.py` | `get` = 10.4 мс (не короткий SELECT) → кэш чтения 2 с внутри хранилища; затем отсутствующий ключ читался из БД всегда → отрицательный кэш | 10.4 → 0.001–0.007 мс (85 мест разом); отсутствующий ключ 1.1 → 0.001–0.002 мс |
| `routes/upload.py` | 4 × `get_all()` в async-роутах → в поток; остальные 19 мест обоснованы построчно | `POST /reprocess-pending` 0.049 с |
| `routes/chunks.py`, `chat_service.py`, `main.py`, `hot_folder_watcher.py`, `routes/watchers.py`, `routes/knowledge_graph.py`, `indexing/knowledge_graph.py` | горячие пути в поток + флаги (`truncated`, `scroll_limit`) | `/chunks` total 3495; чат-мета 4.2 с / 47 документов |

SLA по замерам: **`/health` под нагрузкой чата ≤ 10 мс**.

Уроки (записаны в скилл `kag-admin-debug-deploy`):

- одинаковый вызов стоит по-разному в процессе API и в воркере Celery (solo-пул);
- AST-правка массово: оборачивать весь `ast.Call` целиком (цепочка `.attr` ломается);
  после массовой правки сервиса — дымовой тест по всем затронутым методам;
- `HTTPException` внутри `try` не должна попадать в широкий `except`;
- решать по замеру: `config_store.get` 10.4 мс, `repo.get` 1.3 мс, `repo.upsert` 5.0 мс;
- при правках файлов скриптом нормализовать окончания строк (якоря CRLF/LF).

## Открыто (по убыванию полезности)

1. **Построчные обоснования по остатку карты** — в момент правки файла, не отдельным
   проходом. Сделано: `upload.py` 19, `admin_models.py` 43 (правок не потребовалось —
   все находки либо в потоке, либо дешёвые по замеру). Осталось: `document_service.py` 16,
   `main.py` 14, `routes/knowledge_graph.py` 11. Формат: строка | вызов | замер | решение | почему.
2. Searchable PDF для сканов: наложить OCR-текст (`data/ocr_results/*.pdf.md`) на
   страницы исходника — у сканов нет текстового слоя.
3. Чистка git-истории от утёкших секретов (Neo4j — 16 коммитов, sudo — 2, JWT — 2);
   ротация `ADMIN_PASSWORD`, `QDRANT_API_KEY`, `NEO4J_PASSWORD`.
4. `data: dict` без Pydantic и отсутствие `response_model` в остатке `admin_models.py`;
   разбор `admin_models.py` на модули (2542 строки).

## Как возобновить

1. `git -C C:\VSCODE_PROJECT\kag-system-enhanced log --oneline -3` и `git status` —
   сверить с этим файлом.
2. `python scripts/scan_sync_io.py src` — текущая карта (166 мест).
3. Прочитать `docs/guides/sync-io-audit.md` — что закрыто, замеры, обоснование остатка.
4. Дальше по бэклогу, правило «один шаг»: изменение → docs/скилл → коммит/пуш.

## Инварианты (не нарушать)

- Файлы пользователя не удалять; сироты — в `data/orphans_removed`.
- Секреты не хардкодить в командные строки (только env/stdin); не печатать в чат;
  в репозитории и скиллах секретов нет — тест-страж `tests/test_no_secrets_in_repo.py`.
- Деплой: локально build → push → на 18 `git pull` → `docker-compose pull` →
  `up -d --force-recreate api` (worker с идущей обработкой не трогать) → health.
- Правки больших файлов — отдельным скриптом-файлом (без heredoc), проверка грепом
  по строкам, затем живая проверка на стенде.
- Файлы внутри project-local `.hermes/` нередактируемы для агента — не тратить на них
  попытки (этот handoff поэтому и лежит в `docs/`).
