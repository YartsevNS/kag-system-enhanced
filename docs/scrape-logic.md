# Логика скачивания документов (Web Monitor Scrape)

## Алгоритм для каждого найденного URL

```
URL со страницы
    │
    ├── URL есть в _seen_urls?
    │   ├── НЕТ → скачиваем (GET)
    │   └── ДА ──→ есть в _hash_cache?
    │               ├── НЕТ → скачиваем (GET)
    │               └── ДА ──→ HEAD запрос (без тела)
    │                           │
    │                           ├── Content-Length совпадает?
    │                           │   И
    │                           │   Last-Modified совпадает?
    │                           │   │
    │                           │   ├── ДА → ПРОПУСК (файл не изменился)
    │                           │   └── НЕТ → скачиваем (GET)
    │                           │
    │                           └── HEAD упал (ошибка) → скачиваем (GET)
    │
    └── GET → тело файла → SHA-256
                │
                ├── upload_document() проверяет хеш
                │   ├── Хеш уже есть → возвращает existing (дубликат)
                │   └── Хеш новый → создаёт документ, ставит в Celery
                │
                └── _hash_cache[url] = {size, modified, filename, hash}
                    _seen_urls.add(url)
```

## Проверка на дубликаты при загрузке

```
upload_document(file_content)
    │
    ├── SHA-256(file_content)
    │
    └── _find_by_hash(sha256)
        ├── ЕСТЬ → возвращает существующий DocumentRecord
        └── НЕТ → создаёт новый документ
```

## Хеш-кеш (URL → метаданные)

Хранится в `system_configs` → `web_monitor:state`:
```python
_hash_cache = {
    "https://rst.gov.ru/file/load/12345": {
        "size": "749990",
        "modified": "Wed, 17 Jun 2026 12:00:00 GMT",
        "filename": "gost-r-59162-2020.pdf",
        "hash": "a1b2c3d4e5f6..."
    }
}
```

## Где живут данные

| Данные | Где хранятся | Ключ |
|--------|-------------|------|
| URL → метаданные | PostgreSQL (system_configs) | `web_monitor:state` → `_hash_cache` |
| История скачиваний | PostgreSQL (system_configs) | `web_monitor:downloads` |
| Документ → хеш | PostgreSQL (system_configs) | `documents:{id}` → `file_hash` |
| Документ → статус | PostgreSQL (system_configs) | `documents:{id}` → `status` |

## Важные правила

1. **SHA-256 всегда**, даже для user upload — хеш проверяется ДО сохранения файла
2. **HEAD перед GET** — только для scrape, без тела (экономия трафика)
3. **force=true** (♻️) — очищает `_seen_urls` и `_hash_cache`, все файлы перепроверяются
4. **_save_state** — сохраняет кеш каждые 10000 URL (последние)
5. **`document_service` import** — всегда внутри try блока
