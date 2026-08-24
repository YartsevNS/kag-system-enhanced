# Гид: деплой на сервер

> Сервер: 192.168.50.18 (внутренний), SSH `yartsevn@` по ключу.
> Внешний: qd.gostsecret.ru (nginx :80/:443).

## Бэкап документов (2026-08-24)

- Админка → кнопка «💾 Скачать документы (backup)» рядом с «Переиндексировать».
- API: `GET /api/v1/admin/models/backup-documents` (admin-роль) → ZIP:
  `documents/{id}_{filename}` (файлы из /app/data/uploads) + `documents_meta.json`
  (метаданные всех документов из DocumentRepository).
- ZIP формируется во временном файле, отдаётся FileResponse и удаляется после отправки.
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
docker exec kag-kag-db psql -U kag -d kag -t -c "SELECT status, count(*) FROM documents GROUP BY status;"
```
