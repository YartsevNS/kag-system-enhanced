# Гид: веб-монитор (источники, скрейпинг, ЦБ)

> Актуально на 2026-08-23.

## Где что

- Роуты: `src/api/routes/web_monitor.py` (GET/POST /api/v1/monitor/sources, /check, /history, /builtin, /add-cbr).
- Сервис: `src/api/services/web_monitor.py` (WebMonitorService, BUILTIN_SOURCES, run_check).
- Задача: `src.indexing.tasks.run_monitor_check` (Celery).
- Хранилище: config_store (`web_monitor/sources`, `web_monitor/state`, `web_monitor/history`, `web_monitor/downloads`).

## Типы источников

- **rss** — RSS-ленты (ЦБ, Securelist и т.д.)
- **scrape** — HTML-скрейпинг по css_selector (основной для ЦБ актов)
- **browser** — Playwright (⚠️ НЕ работает — Playwright не установлен)
- **change** — мониторинг изменений

## ЦБ РФ — акты по информационной безопасности (важно)

- URL: `https://www.cbr.ru/information_security/acts/`
- Тип: scrape, css_selector `a[href*='/Crosscut/LawActs/File/']`
- Пагинация: `https://www.cbr.ru/Crosscut/LawActs/Page/95016?Date.Time=Any&Page={page}` (max 8 страниц)
- **Лимит: ~4-5 файлов/мин** (item_delay=15с, batch_size=3) — иначе cbr.ru блокирует
- file_types: .pdf, .docx, .doc
- ВАЖНО: `_download_and_upload` импортирует `document_service` ВНУТРИ try — НЕ выносить наружу (иначе ошибка "cannot access local variable 'document_service'").

## Ключевые моменты run_check

- `force=True` сбрасывает `_seen_urls = set()` и `s.last_hash = None` — иначе все URL из seen пропускаются.
- Фильтр: enabled + check_interval_minutes (для ЦБ 720 мин = 12 ч) — если не прошёл интервал, источник пропускается.
- Результат: MonitorResult (items, new_items, skipped_items, error).
- История: `web_monitor/history` (последние 500 записей).

## Проверка источника вручную

```bash
# Через Celery (не принимает force)
docker exec kag-api python -c "from src.indexing.tasks import run_monitor_check; print(run_monitor_check.delay(source_id='<id>').id)"

# Напрямую с force (в контейнере api, detached — не блокирует worker)
docker exec -d kag-api bash -c 'python -u -c "
import asyncio
from src.api.services.web_monitor import web_monitor
async def main():
    res = await web_monitor.run_check(source_id=\"<id>\", force=True)
    for r in res:
        print(r.status, \"new:\", r.new_items, \"err:\", r.error, flush=True)
asyncio.run(main())
" > /tmp/check.log 2>&1'
# прогресс:
docker exec kag-api tail -5 /tmp/check.log
```

## Остановка фоновой проверки

⚠️ `pkill -f 'monitor/check'` убивает СВОЙ ssh (паттерн в командной строке). Находить PID и kill по нему:
```bash
docker exec kag-api ps aux | grep -E 'monitor' | grep -v grep
docker exec kag-api kill <PID>
```

## Скрейперы (из git log)

- cit.cap.ru: `a[href*=fs.cap.ru/file/], a[href*=docs.cntd.ru/document/]` (fs.cap.ru — Angie, HEAD 405, GET OK; docs.cntd.ru — 302 на SSO, текст через `?print=1`/`?full=1` в `#textBlock1`).
- ЦБ: см. выше.

## Диагностика

```bash
# Список источников
docker exec kag-api python -c "from src.api.services.config_store import config_store; [print(s.get('id'), s.get('name'), s.get('enabled')) for s in (config_store.get('web_monitor','sources') or [])]"
# История
docker exec kag-api python -c "from src.api.services.config_store import config_store; print(config_store.get('web_monitor','history'))"
# Очередь загрузок
docker logs kag-worker | grep -E 'Загружен|Дубликат|web_monitor'
```

## web_collector — отдельный проект (2026-08-27)

Скрапинг/скачивание вынесено в ОТДЕЛЬНЫЙ проект `C:\VSCODE_PROJECT\web_collector`
(собственный git-репозиторий). В KAG его кода нет — только совместимость API:
- POST /api/v1/upload принимает `source_name` / `source_url` (Form) →
  `source_metadata` документа (подпись «Источник: Y» в UI).
- Фикс document_service.upload_document: `source_metadata=source_metadata`
  при создании DocumentRecord (ранее терялся у всех загрузок).
- Для внешних клиентов: multipart-поля с `charset=utf-8` (иначе кириллица
  декодируется как cp1251 → мусор).
- Служебный пользователь KAG для внешней загрузки: `kag_collector`
  (креды в web_collector/.env отдельного проекта, в git не коммитить).

Всё полезное из web_collector постепенно переносится обратно в web_monitor
(retry/backoff, проверка магических байтов и т.д.).

## 5 методов источников (2026-08-28)

Источники разделены по методу получения (секции в /monitor, сворачиваемые):

| Тип | Метод | Описание |
|---|---|---|
| rss | 📡 RSS | лента новостей (feedparser, вложения/текст) |
| scrape | 🕷 Скрапинг | страница со ссылками на файлы (.pdf/.docx), css_selector, file-service |
| browser | 🌐 Browser | JS/SPA-сайты через Playwright (перехват network) |
| change | 🔄 Change | отслеживание изменений страницы (сохраняет целиком) |
| api | ⚙️ API (JSON) | JSON-эндпоинт: GET → JSON → записи {url/title/download_url} |

API-метод: `json_path` (путь к массиву записей, напр. "data.items"; пусто = первый
массив), пагинация через `pagination_url` с {page}. Приоритет ссылки:
download_url > url > href > link; для GitHub contents API пропускаются папки (type=dir).

Форма добавления показывает поля под выбранный метод. json_path/sample_limit
сериализуются в config_store (был баг: терялись при save/load).

## Лента новостей /news (2026-08-28)

/news показывает НАЙДЕННЫЕ RSS-новости из реестра web_monitor:
- _check_rss сохраняет каждую найденную запись в config_store `web_monitor`/`news_items`
  (title, text, url, source_name, published, found_at; дедуп по url, лимит 500)
- GET /api/v1/monitor/news?limit=&source=&q= — список (сортировка found_at desc)
- /news больше НЕ создаёт временные источники (раньше создавал temp-* на каждый
  просмотр — мусор в мониторе); источники ленты = RSS-источники web_monitor,
  «Добавить» создаёт rss-источник в мониторе, toggle/remove — enable/delete.
