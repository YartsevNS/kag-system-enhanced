# BUGS_AND_ISSUES.md

## Исправлено
- ✅ Occular OCR — интегрирован и работает
- ✅ Upload через очередь (asyncio.Queue + 4 workers)
- ✅ Structured errors с upload_id на фронте

## Критические
1. Upload не готов к 1GB файлам — нет chunked upload, нет streaming
2. Bulk folder import — нет загрузки папками/архивами
3. Нет Celery для изоляции фоновых задач

## Высокий приоритет
- Rate limiting на upload endpoint
- Path traversal protection
- Progress bar на фронте (убрали, но нужен для UX)
- Retry упавших документов (автоматический)