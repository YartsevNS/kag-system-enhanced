# KAG System Improvement Plan

## Current Status
Branch: feature/ocr-upload-improvements
Файлы: до 1GB, много мелких и средних.
Occular OCR: ✅ интегрирован.
Очередь обработки: ✅ asyncio.Queue + 4 workers.

## Priority 1: Стабильность Upload
- Chunked upload для файлов >100MB (TUS/resumable.js)
- Streaming upload с лимитом 1GB
- Bulk folder import (zip/tar или папка)
- Rate limiting 10/min на upload

## Priority 2: Фоновая обработка (Celery)
- Вынести process_document в Celery worker
- Авто-retry упавших задач (3 попытки)
- Мониторинг очереди (длина, время обработки)

## Priority 3: Progress Bar + UX
- Вернуть прогресс-бар (но простой, без UploadError)
- Показывать статус обработки через поллинг /list
- Кнопка "Повторить" для упавших документов

## NEXT-2026-05 TODO
- [ ] Chunked upload (TUS protocol)
- [ ] Rate limiter на POST /upload
- [ ] Celery worker + Redis broker
- [ ] Retry failed documents (3 attempts, exponential backoff)
- [ ] Bulk folder import
- [ ] Progress bar на фронте (минимальный)
- [ ] Reranker (лёгкий, без torch)