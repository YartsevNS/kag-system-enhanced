# Know — KAG knowledge base

## Архитектура загрузки документов (v2 — streaming + очередь)

### Путь файла от браузера до диска
```
Браузер → POST /api/v1/upload/ → kag-api (FastAPI)
  → Стриминг по 64KB в /tmp/uploads/{upload_id}_{filename}
  → Валидация (размер, тип, безопасность)
  → document_service.upload_document()
    → SHA-256 хеш стримингом (не весь файл в память)
    → Проверка дубликатов по хешу
    → os.rename() в /app/data/uploads/{doc_id}_{filename}
  → Асинхронная очередь:
    → await _processing_queue.put(doc_id)
    → Worker (asyncio, max_workers из env)
    → process_document() — парсинг → чанкинг → векторизация
```

### Ключевые файлы
- `src/api/routes/upload.py` — POST /upload, POST /batch, очередь, воркеры
- `src/api/services/document_service.py` — upload_document (streaming), cleanup
- `src/api/static/documents.html` — фронт с uploadFileXHR + UploadError
- `src/models.py` — DocumentStatus (добавлено upload_id)

### Streaming upload — детали
- `TEMP_DIR = /tmp/uploads` — временная директория
- `MAX_FILE_SIZE = 500MB` — лимит, проверяется в процессе
- `CHUNK_SIZE = 65536` (64KB) — размер буфера
- `STALE_TEMP_MINUTES = 30` — автоочистка
- Файл стримится через `await file.read(CHUNK_SIZE)` в цикле
- При ошибке любого этапа — `_cleanup_temp()` гарантированно удаляет temp-файл
- После успешной проверки — `os.rename()` (атомарно на одной ФС)
- Если rename не сработал (cross-fs) — `shutil.copy2()` + unlink

### Очередь обработки
- `asyncio.Queue()` — FIFO, producer = upload, consumer = worker
- `_MAX_WORKERS = int(env.MAX_WORKERS) or min(4, cpu_count())`
- `ProcessPoolExecutor(max_workers=_MAX_WORKERS)` — для CPU-bound задач (OCR, pdf2image)
- `_worker_loop(worker_id)` — вечный цикл: get() → process_document() → task_done()
- Стартуют при импорте модуля: `asyncio.ensure_future(_worker_loop(i))`

### Cleanup temp-файлов
- `DocumentService.cleanup_stale_temp_files(temp_dir, max_age_minutes)`
- Удаляет файлы по mtime старше N минут
- Вызывается: перед каждым upload как предочистка
- В будущем: фоновый таймер из lifespan

### Ошибки на фронте
- `UploadError` — класс с полями: `code`, `status` (HTTP), `uploadId`
- Коды: `VALIDATION_ERROR`, `UPLOAD_ERROR`, `HTTP_ERROR`, `NETWORK_ERROR`, `PARSE_ERROR`
- Ошибка НЕ скрывается — остаётся в прогресс-баре красным текстом
- Тост тоже красный (второй параметр `toast(msg, true)`)

### Установка MAX_WORKERS
```bash
# В .env на сервере или в docker-compose environment
MAX_WORKERS=4
```
По умолчанию: `min(4, cpu_count())` — для сервера с 4 ядрами = 4

### Occular-ocr — инициализация

**Файл**: `src/indexing/hybrid_parser.py`

**Проблема**: `OCRPipeline.__init__() got an unexpected keyword argument 'max_workers'`
Новая версия `ocr_skel` убрала параметр `max_workers`. OCRPipeline сам управляет потоками через onnxruntime.

**Решение (строка 76)**:
```python
# Было (сломано):
self._ocular = OCRPipeline(onnx=True, gpu=False, max_workers=workers)

# Стало (работает):
self._ocular = OCRPipeline(onnx=True, gpu=False)
```

**Fallback цепочка**:
1. Docling + Occular-ocr (приоритет) — layout + распознавание русского текста
2. Docling only — если Occular не доступен
3. Tesseract — последний fallback (МЕДЛЕННЫЙ, не используется если Occular жив)

## UploadError — class hoisting в JavaScript

**Файл**: `src/api/static/documents.html`

**Проблема**: `ReferenceError: UploadError is not defined`
class declarations в JS не hoist'ятся (в отличие от function declarations). `uploadFileXHR` (function declaration) использует `new UploadError()`, но class определён после функции.

**Решение**: класс `UploadError` определён ДО (`строка 378`), `uploadFileXHR` — ПОСЛЕ (`строка 399`).

**Структурированная ошибка**:
```javascript
class UploadError extends Error {
  constructor(message, code, status, uploadId) { ... }
}
// code: VALIDATION_ERROR | UPLOAD_ERROR | HTTP_ERROR | NETWORK_ERROR | PARSE_ERROR
```

## Структурированный ответ сервера при ошибках upload

**Файл**: `src/api/routes/upload.py`

**Формат ошибки**:
```json
{
  "code": "VALIDATION_ERROR",
  "message": "Файл слишком большой: 500MB макс.",
  "upload_id": "58ae0c15-d74f-474a-91ed-8834518942b3"
}
```

**Где генерируется**:
- `SecurityValidationError` → `VALIDATION_ERROR` (400)
- `HTTPException` с лимитом размера → `413`
- Любая другая ошибка → `UPLOAD_ERROR` (500)

На фронте парсится в `uploadFileXHR()` через `err.detail?.message || err.message` и пробрасывается в `UploadError`.

## План дальнейших улучшений
1. Chunked upload (TUS/resumable.js) для файлов >500MB
2. Hot folder (watchdog на /data/hot/)
3. Метрики: uploads_total, uploads_failed, queue_depth, disk_usage
4. Авто-ретрай с идемпотентностью и экспоненциальной задержкой
5. Rate-limit на эндпоинт upload
