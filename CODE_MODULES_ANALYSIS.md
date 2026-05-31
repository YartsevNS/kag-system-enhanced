# CODE_MODULES_ANALYSIS — kag-system-enhanced (branch kag-rusystem-allv3)

## Общая структура

- `src/api/` — FastAPI endpoints
- `src/indexing/` — OCR, parsing, chunking, vectorization
- `src/security/` — GOST, auth
- `src/models.py` — отражает UI страницы

## Ключевые выводы

1. **Upload Flow** — критически слаб для больших файлов (1GB+).
2. OCR только pytesseract — нужно Occular.
3. Много hardcoded Docker-сервисов.

**Рекомендации:**
- Streaming upload + Celery.
- Bulk folder import.
- Occular OCR integration.