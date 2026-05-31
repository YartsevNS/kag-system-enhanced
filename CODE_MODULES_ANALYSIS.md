# CODE_MODULES_ANALYSIS — kag-system-enhanced (branch feature/ocr-upload-improvements)

## Общая структура

- `src/api/` — FastAPI endpoints
- `src/indexing/` — OCR, parsing, chunking, vectorization
- `src/security/` — GOST, auth
- `src/models.py` — Pydantic модели

## Ключевые выводы

1. **Upload Flow** — не готов к 1GB файлам, нет chunked upload.
2. **OCR** — Occular интегрирован, работает через onnxruntime.
3. **Очередь** — asyncio.Queue + 4 workers, но нет изоляции (Celery).
4. **Reranker** — удалён (тяжёлый torch), нужна лёгкая альтернатива.

## Рекомендации

- Chunked upload (TUS) + streaming
- Celery для фоновых задач
- Лёгкий reranker (без torch)
- Progress bar на фронте