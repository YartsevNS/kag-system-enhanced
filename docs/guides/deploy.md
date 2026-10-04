# Гид: деплой на сервер

> Сервер: 192.168.50.18 (внутренний), SSH `yartsevn@` по ключу.
> Внешний: qd.gostsecret.ru (nginx :80/:443).

## Деплой из готовых образов (новая схема, 2026-09)

api/worker/mcp-server больше НЕ собираются на сервере — код зашит в образы
на Docker Hub (kre44et/kag-*:2026.09.07, сборка локально/CI). Развёртывание:

```bash
# 1. На сервере: подтянуть новый compose + .env
git pull origin PREPROD
docker-compose pull      # тянет kre44et/kag-api, kag-worker, kag-mcp и инфраструктуру
docker-compose up -d
```

- Данные (./data, ./user_data, volumes PG/Qdrant/Neo4j) не трогаются.
- Обновление кода = пересобрать образ (docker build -t kre44et/kag-api:<tag>)
  и запушить, затем на сервере pull.
- DEV-режим с живым ./src: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d`
  (сборка из локальных Dockerfile + монтирование кода).

### Порядок сборки образов (kag-base → тонкие)

api/worker/mcp наследуют общий базовый образ `kre44et/kag-base:<tag>`: python 3.11-slim + apt +
requirements + **RapidOCR/PP-OCRv5 с предзагруженными весами** + Playwright Chromium.
Occular, torch и pyctcdecode из базы УБРАНЫ (лицензия OpenRAIL-M и ~1,7 ГБ веса).
Порядок строгий:

```bash
# 1. База — ТОЛЬКО при изменении requirements.txt / весов / Dockerfile базы (~5-10 минут)
docker build -f docker/base/Dockerfile -t kre44et/kag-base:<TAG> .
docker push kre44et/kag-base:<TAG>

# 2. Тонкие образы — при любом изменении кода (секунды).
#    ОБЯЗАТЕЛЬНО сначала обновить FROM в git (Dockerfile, Dockerfile.worker, Dockerfile.mcp),
#    а НЕ правкой в worktree стенда — иначе образ соберётся от старой базы.
docker build -t kre44et/kag-api:<TAG> -f Dockerfile .
docker build -t kre44et/kag-worker:<TAG> -f Dockerfile.worker .
docker build -t kre44et/kag-mcp:<TAG> -f Dockerfile.mcp .
docker push kre44et/kag-api:<TAG> && docker push kre44et/kag-worker:<TAG> && docker push kre44et/kag-mcp:<TAG>

# 3. На стенде: теги в docker-compose.yml, затем
docker-compose up -d --force-recreate api worker worker-maintenance mcp-server
#    ВНИМАНИЕ (проверено 04.10.2026): compose v1 без --no-deps пересоздаёт и ЗАВИСИМОСТИ api —
#    postgres, redis, qdrant перезапускаются вместе с ним (в том прогоне были «Up 6 days»,
#    стали «Up 2 minutes»). Данные в томах при этом целы (248 документов, граф, 10 278 точек),
#    но очередь Celery в redis обнуляется, а идущая обработка потеряет замок QueueGuard.
#    Если прерывать БД/очередь нельзя — добавлять --no-deps.

# 4. Проверка: api=200, mcp=200, docker ps — все healthy
```

**Предзагрузка весов обязана покрывать ОБА языка**: текст страницы — `cyrillic`, текст ячеек —
`eslav`. Если весов языка в образе нет, RapidOCR тянет их в рантайме с modelscope.cn и в контейнере
падает с PermissionError (пишет в site-packages, принадлежащий root) — в закрытом контуре путь
ячеек становится мёртвым. Проверка после сборки:
`docker run --rm -e HF_HUB_OFFLINE=1 --entrypoint sh kre44et/kag-base:<TAG> -c 'ls /usr/local/lib/python3.11/site-packages/rapidocr/models/'`
— должно быть 5 файлов: оба распознавателя (cyrillic и eslav), детектор, классификатор поворота и
распознаватель PP-OCRv6 (в нашей схеме не задействован).

Экономия: api/worker/mcp делят один тяжёлый базовый слой вместо ~7 ГБ дублей.
Текущие теги (04.10.2026): `kag-base`, `kag-api`, `kag-worker`, `kag-mcp` = **2026.10.04.2**.

## Состав стенда 18 (проверено 04.10.2026)

Всего **13 контейнеров**: 5 образов наши (`kre44et`), 8 — сторонние.

Наш код:

| Контейнер | Образ | Назначение |
|---|---|---|
| `kag-api` | kre44et/kag-api:2026.10.04.2 | FastAPI: API, веб-страницы, чат (внутр. порт 8000) |
| `kag-system_worker_1` | kre44et/kag-worker:2026.10.04.2 | Celery: обработка документов (разбор, OCR, чанки, векторы, граф) |
| `kag-system_worker-maintenance_1` | kre44et/kag-worker:2026.10.04.2 | Celery: обслуживание — скан сирот, кандидаты алиасов, ночные задачи |
| `kag-mcp` | kre44et/kag-mcp:2026.10.04.2 | MCP-сервер (порт 8001) |
| `kag-neo4j` | kre44et/dozerdb:5.26.27.0 | Neo4j CE 5.26 + DozerDB + APOC — граф знаний (7474/7687) |

Инфраструктура (сторонние образы):

| Контейнер | Образ | Роль |
|---|---|---|
| `kag-nginx` | nginx:1.25-alpine | вход 80/443, прокси на api (qd.gostsecret.ru) |
| `kag-postgres` | postgres:16-alpine | документы, настройки, пользователи, чаты |
| `kag-qdrant` | qdrant/qdrant:v1.12.1 | векторное хранилище (фрагменты, карточки) 6333/6334 |
| `kag-redis` | redis:7-alpine | очередь Celery и замки QueueGuard (6379) |
| `kag-keycloak` | keycloak:24.0 | SSO-вход и пользователи (8080) |
| `kag-prometheus` | prom/prometheus:v2.49.0 | метрики (9090) |
| `kag-grafana` | grafana/grafana:10.2.0 | дашборды (3000) |
| `kag-gigachat-proxy` | ghcr.io/ai-forever/gpt2giga | мост к GigaChat |

Данные живут ВНЕ контейнеров — смена образов их не трогает:
- тома: `kag-system_neo4j_data`, `kag-system_kag_db_data`, `kag-system_qdrant_data`,
  `kag-system_redis_data`, `kag-system_prometheus_data`, `kag-system_grafana_data`,
  `kag-system_loki_data` (том есть, контейнера Loki нет — см. техдолг по наблюдаемости),
  `kag-system_neo4j_logs`;
- каталоги: `./data` (uploads, ocr_results, models) и `./user_data`.

### Правило имён контейнеров: `kag-<роль>`

`kag-api`, `kag-mcp`, `kag-nginx`, `kag-postgres`, `kag-qdrant`, `kag-redis`, `kag-neo4j`,
`kag-keycloak`, `kag-prometheus`, `kag-grafana`, `kag-loki`, `kag-otel-collector`,
`kag-gigachat-proxy`. Префикс обязателен: имя контейнера уникально на ХОСТЕ, а продукт ставится на
чужие серверы, где рядом могут жить другие стеки (голые `postgres`, `redis`, `nginx` столкнутся).

**Исключение — `worker` и `worker-maintenance`:** им `container_name` НЕ задаётся, иначе ломается
масштабирование `--scale worker=N` (имена контейнеров уникальны). Compose называет их
`kag-system_worker_<N>` и `kag-system_worker-maintenance_<N>` — по имени проекта. Это не недосмотр,
а требование масштабирования (фича есть в админке).

Переименование `container_name` НЕ затрагивает имена сервисов, а именно по ним контейнеры видят друг
друга: prometheus скрейпит `api:8000`, grafana ходит на `http://prometheus:9090` и `http://loki:3100`,
nginx — на upstream по именам сервисов, api — на `kag-db`, `qdrant`, `redis`, `neo4j`.

04.10.2026 контейнер БД переименован `kag-kag-db` → `kag-postgres` (том `kag-system_kag_db_data` не
менялся, данные целы: 248 документов, 873 таблицы, база Keycloak на месте).

Внешние зависимости стенда:
- **служба OCR PP-OCRv5 на сервере моделей 41 (:8020) — ускоритель, а не зависимость**: тот же
  движок с весами обоих языков вшит в `kag-base` и работает офлайн (проверено 04.10.2026 при
  `HF_HUB_OFFLINE=1`: та же накладная — 292 строки локально против 292 строк через службу;
  по времени локально ~18 с против ~3 с). Если служба недоступна, скан распознаётся локально;
- провайдеры LLM для чата/анализа документов/графа/классификации — настраиваются в админке;
- Ollama на 41 с моделями зрения — опция «таблицы без линий» (по умолчанию выключена).

## Бэкап документов (2026-08-24)

- Админка → кнопка «💾 Скачать документы (backup)» рядом с «Переиндексировать».
- API: `GET /api/v1/admin/models/backup-documents` (admin-роль) → ZIP:
  - `documents/{id}_{filename}` — файлы из /app/data/uploads
  - `documents_meta.json` — метаданные документов (DocumentRepository)
  - `aliases.json` — словарь алиасов (entity_aliases, включая pending)
  - `config_store.json` — все настройки (категории из system_configs)
  - `chat_history.json` — chat_sessions + chat_messages
- ZIP формируется во временном файле, отдаётся FileResponse и удаляется после отправки.
- Каждый JSON-блок в try/except — ошибка одного не ломает весь ZIP.
- Файлы: src/api/routes/admin_models.py (backup_documents), src/api/static/admin.html (backupDocuments).

## Правила (критично, из опыта)

1. **scp по одному файлу с полным путём.** `scp a.py b.py c.py user@host:/path/` ПЕРЕЗАПИШЕТ файлы друг другом (последний побеждает). Всегда:
   ```bash
   scp src/a.py user@host:/home/user/proj/src/a.py
   scp src/b.py user@host:/home/user/proj/src/b.py
   ```
   После массового scp проверять: `ls -la` + grep в контейнере.

2. **НИКОГДА sed для .py** (UTF-8) — ломает кодировку. Использовать patch/write_file.

3. **CRLF ломает shebang** в deploy.sh — файл должен быть LF. Есть self-heal в deploy.sh.

4. **Docker маскирует `***`** в командах/выводе — не путать с реальными значениями.

5. **Не удалять kag-system_* образы** — пересборка 40+ мин. `<none>` и чужие проекты чистить можно.

6. **Внешний SSH нестабилен** (77.37.242.130) — при Connection timed out повторять с паузами. Основной: 192.168.50.18.

## Быстрый деплой изменений

```bash
# 1. Синтаксис локально
python -m py_compile src/api/services/document_service.py && echo OK
# 2. По одному файлу
scp src/api/services/document_service.py yartsevn@192.168.50.18:/home/yartsevn/kag-system/src/api/services/document_service.py
# 3. Рестарт нужных контейнеров
ssh yartsevn@192.168.50.18 "docker restart kag-worker kag-api"
# 4. Проверка что файл в контейнере
ssh yartsevn@192.168.50.18 "docker exec kag-api grep -c 'уникальная_строка' /app/src/api/services/document_service.py"
```

`/home/yartsevn/kag-system/src` смонтирован в контейнеры как `/app/src` — статика и .py обновляются без пересборки, только рестарт.

## Деплой через git (полный)

```bash
git add -A && git commit -m "..." && git push origin stable_PyMuPDF
# на сервере:
cd /home/yartsevn/kag-system
git checkout -- .   # сбросить локальные ручные правки (compose может быть пропатчен админкой!)
git pull origin stable_PyMuPDF
# при изменении compose:
docker-compose up -d --no-deps --build api worker
```

⚠️ **Перед pull на сервере: `git checkout -- .`** — админка персистентно патчит docker-compose.yml на сервере; иначе конфликт.

## Worker ресурсы

- Текущие: 4 CPU / 12G (переменные `${WORKER_CPUS:-4.0}` / `${WORKER_MEMORY:-12G}`).
- Менять: админка «Ресурсы Worker» (живой docker update + персистентный патч compose + рестарт).
- Было 4G/2CPU → OOMKilled на сканах (Occular).

## Возобновление после паузы

```bash
docker start kag-worker
# если была остановлена проверка ЦБ — запустить источник заново (Веб-монитор)
```

## Проверка после деплоя

```bash
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'worker|api'
docker logs kag-worker --since 2m | grep -E 'ready|Recovery|error'
docker exec kag-redis redis-cli -n 1 LLEN documents
docker exec kag-postgres psql -U kag -d kag -t -c "SELECT status, count(*) FROM documents GROUP BY status;"
```

## Секреты: только в .env (2026-09-13)

Правило: в репозитории нет ни одного пароля или ключа. Все секреты живут в `.env`
на сервере (генерируются `deploy.sh`) либо в менеджере паролей администратора.

Что сделано, чтобы значение из репозитория не могло стать рабочим:

- **compose требует переменные**: у `JWT_SECRET`, `KEYCLOAK_CLIENT_SECRET`,
  `KEYCLOAK_ADMIN_PASSWORD`, `KC_DB_PASSWORD`, `POSTGRES_PASSWORD`,
  `GRAFANA_ADMIN_PASSWORD`, `NEO4J_AUTH` и `KAG_DB_URL` нет значений по умолчанию —
  стоит `${VAR:?сообщение}`. Если `.env` неполный, `docker-compose up` падает с
  внятным текстом вместо того, чтобы поднять систему на пароле из репозитория
  (раньше там были `supersecretkey`, `change_me`, `admin`, `keycloak_password`).
- **`src/config.py`**: у секретов пустые умолчания. Особенно важно для
  `JWT_SECRET`: литеральный ключ подписи означал бы, что токен администратора
  можно подделать, зная репозиторий.
- **Neo4j**: пароль берётся ТОЛЬКО из `NEO4J_PASSWORD`; фолбэк на литерал убран.
  Мастер инициализации (`setup.py`) больше НЕ перебирает известные пароли и не
  «синхронизирует» пароль запросом, склеенным f-строкой: не подошёл пароль из
  `.env` — администратору выдаётся понятная ошибка.
- **страховка**: `tests/test_no_secrets_in_repo.py` — структурный тест (литеральные
  умолчания секретов в `config.py`, умолчания `${VAR:-<литерал>}` в compose,
  пароли в `PROJECT.md`/`docs`, `.env` под контролем git). Он падает при попытке
  вернуть пароль в код.

**Что важно знать про историю git.** Удаление секрета из текущего состояния НЕ
удаляет его из истории: старые значения (`kagneo4j2026`, `supersecretkey`,
sudo-пароль) остаются в прежних коммитах. Поэтому:

1. правильный ответ — **ротация** таких значений (после неё история безопасна);
2. если репозиторий передаётся заказчику и история важна — либо переписать
   историю (`git filter-repo`, затем force-push и повторный клон на сервере),
   либо отдавать **экспорт без истории** (`git archive` из текущего коммита).

Проверка при добавлении новой настройки-секрета: строка в `.env.example` с
плейсхолдером → `${VAR:?…}` в compose → поле в `config.py` без литерала → строка
в `.env` на сервере → `docker-compose config` проходит.

## Инцидент 2026-09-13: сайт 502 после инкрементального деплоя (nginx держал старый IP)

**Что было.** Инкрементальный деплой `docker-compose up -d --force-recreate api` меняет
IP контейнера. Статический `upstream kag_api { server api:8000; }` в nginx резолвится
ОДИН раз при старте, поэтому nginx продолжал ходить на прежний адрес и отдавал **502 на
весь сайт** (в логе `connect() failed (111: Connection refused) ... upstream: 172.20.0.3`,
при том что контейнер был уже `172.20.0.9`). Проверки `curl localhost:8000/...` при этом
показывали 200 — они идут мимо nginx, поэтому проблему не ловили.

**Как починено (навсегда).** `docker/nginx/conf.d/kag.conf`: адреса upstream вынесены в
переменные, добавлен `resolver 127.0.0.11 valid=10s ipv6=off;` (Docker DNS). С переменной
nginx перерезолвит имя с TTL 10 с и подхватит новый IP сам. Проверено живьём: принудительно
сменили DNS-запись контейнера — сайт восстановился за <=3 с **без reload nginx**; со старым
конфигом он оставался бы в 502 до ручного вмешательства. Плата: `keepalive` до upstream
несовместим с переменной в `proxy_pass` (соединения не переиспользуются — для нашего
профиля несущественно).

**Правила, которые следуют из инцидента:**

1. Проверять деплой **через nginx**, а не только на порту API:
   `curl -s -o /dev/null -w "%{http_code}" http://localhost/api/v1/health` и `https://localhost/`.
2. Если правите `kag.conf` — сначала `docker exec kag-nginx nginx -t` (валидация), потом
   `nginx -s reload`. Невалидный конфиг reload отклоняет, старый продолжает работать —
   это спасает от падения сайта из-за опечатки.
3. Никогда не переподключать контейнер вручную без алиаса: `docker network connect` БЕЗ
   `--alias api` оставляет nginx без DNS-записи -> 502. Правильно:
   `docker network connect --alias api kag_internal kag-api` (или пересоздать сервис через
   compose, который алиасы ставит сам).
