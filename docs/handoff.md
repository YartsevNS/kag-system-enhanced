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
  `kag-system_worker_1`, `worker-maintenance` на образе **2026.09.13.44**.
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

## Тесты: набор полностью зелёный (2026-09-13)

**Полный прогон: 380 passed, 0 failed** (было 4 failed, 376 passed). Разбор:

| Тест | Причина | Правка |
|---|---|---|
| `test_auth.py::test_me_wrong_secret` | `from jose import jwt`, а python-jose в зависимостях нет | переведено на PyJWT |
| `test_kg_admin.py::test_admin_ops_forbidden_for_regular_user` | в `ADMIN_OPS` пути `/kg/watchdog/start|stop`, которых нет (есть `GET /watchdog/status`, `POST /type-watchdog/start`) → 404 вместо 403 | список сверен с роутером; стража проверена на реальных роутах |
| `test_security.py::test_generate_key` | два `GOSTCrypto()` с путём по умолчанию читали ОДИН файл ключа → «ключи разные» падало | `tmp_path` на экземпляр + проверка, что тот же файл даёт тот же ключ |
| `test_security.py::test_rate_limit_exceeded` | 150 запросов, растянутых на 150 с, при окне 60 с → в окне 60 < 100, лимит не превышен | 150 запросов в 30 с > 100 |

Плюс ранее в этот день: `test_api.py` 7 падений → 16 passed, `test_monitoring.py` 18 → 32
passed (причина: auth-middleware + SetupCheck + пустая тестовая SQLite из-за StaticPool и
неимпортированных `monitoring_models`).

**Где что лежит:** токен для тестов — фикстура `auth_headers` в `tests/conftest.py` (секрет
— уникальный фейк на прогон через `monkeypatch`). Подмена `_is_configured` — в фикстуре
`client` файла `test_monitoring.py` (снимается автоматически).

**Урок:** красный набор скрывал два реальных бага (`folder_watcher.remove_folder` → 500,
`doc-1`/`doc-10_other.pdf`). Ни один из 22 починенных тестов не удалён и не пропущен.

## Сессия 2026-09-13 (продолжение): тесты, folder_watcher, скилл

- **`src/monitoring/folder_watcher.py` — реальная ошибка, найденная тестом:**
  `Observer.unschedule()` требует `ObservedWatch` (объект от `schedule()`), а код передавал
  `FolderWatcherHandler` → `KeyError` из watchdog → **500** при снятии папки с наблюдения
  (`DELETE /api/v1/watchers/folders/{id}`). Исправлено: `_watches` хранит watch по папке;
  `start()` больше не хардкодит `recursive=True`. Проверено живьём: добавление и снятие
  папки → 200, список пуст.
- **Тесты починены, набор зелёный:** `tests/test_api.py` 7 падений → 16 passed,
  `tests/test_monitoring.py` 18 → 32 passed. Причины были не «в коде»: тесты писались до
  auth-middleware (ждали 200 без токена), патчили несуществующие атрибуты, а тестовая
  SQLite была пустой (in-memory живёт НА СОЕДИНЕНИЕ — нужен `StaticPool`, плюс импорт
  `monitoring_models`, иначе таблиц нет в схеме) и SetupCheck уводил запросы на `/setup`.
- **Скилл `kag-admin-debug-deploy` разбит**: SKILL.md 98 890 → 27 496 символов (упёрся в
  лимит 100k, правки не проходили). История вынесена в `references/`:
  `setup-wizard-and-secrets.md`, `deploy-recipes.md`, `history-incidents-2026-06-07.md`,
  `hybrid-search.md` (дословно), `nginx-upstream-resolver.md`. Бэкап:
  `SKILL.md.before-split-2026-09-13`. Указатель переписан, в архиве ~191 файл.

## Инцидент 2026-09-13 (сайт лежал): nginx держал старый IP api

`--force-recreate api` меняет IP контейнера, а статический upstream nginx резолвится один
раз при старте -> **502 на весь сайт** до ручного `nginx -s reload`. Починено навсегда:
в `docker/nginx/conf.d/kag.conf` адреса upstream в переменных +
`resolver 127.0.0.11 valid=10s ipv6=off` — nginx перерезолвит сам (проверено: сайт
восстановился за <=3 с без вмешательства). Разбор — `docs/guides/deploy.md`.

Уроки: (1) проверять деплой ЧЕРЕЗ nginx, а не только `localhost:8000` — мои проверки шли
мимо nginx и 502 не видели; (2) `docker network connect` без `--alias api` ломает
DNS-запись -> 502; (3) `nginx -t` перед reload.

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
