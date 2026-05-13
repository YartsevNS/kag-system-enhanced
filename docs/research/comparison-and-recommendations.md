# Сравнительный анализ и рекомендации

## Сводка по проектам

| | Docling | paperless-ngx | kag-system (наш) |
|---|---|---|---|
| Звёзды | 59.7k | 40.6k | — |
| Парсинг PDF | ⭐⭐⭐⭐⭐ (VLM) | ⭐⭐⭐ (Tesseract) | ⭐⭐ (базовый) |
| OCR русский | Через EasyOCR | Tesseract (46%) | Occular-ocr (93.7%) |
| Векторный поиск | Нет | Нет | Qdrant (⭐) |
| RAG/Чат | Через интеграции | Нет | FastAPI + Ollama |
| Multi-user | Нет | Object-level RBAC | Group-level RBAC |
| Классификация | Нет | Правила + ML | Нет |
| Мониторинг | Нет | Папки + email | Папки + web + уведомления |
| API | Docling-serve | REST | FastAPI |
| UI | Нет | Django | Custom Linear |

## План улучшений kag-system

### Фаза 1: Документный интеллект (Docling)
1. **Docling как основной парсер** — замена PyPDF2/python-docx на Docling
   - Table extraction из PDF
   - Reading order detection
   - Formula recognition
   - Структурированный вывод (Markdown)
2. **VLM для сложных сканов** — GraniteDocling-258M
   - Диаграммы и графики → текст
   - Рукописный текст

### Фаза 2: Классификация (paperless-ngx)
1. **Document types** — счёт, договор, анкета, письмо
2. **Auto-tagging rules** — правила на основе содержимого
3. **ML-классификация** — обучение на основе ручной разметки
4. **Correspondents** — отслеживание отправителей

### Фаза 3: Продвинутый ingestion (paperless-ngx)
1. **Email ingestion** — забор документов из IMAP
2. **Consumption templates** — шаблоны обработки
3. **Object-level permissions** — права на уровне документов

### Фаза 4: Архитектурные улучшения
1. **Docling MCP server** — интеграция через MCP протокол
2. **Асинхронная очередь Docling** — через Celery (уже есть)
3. **Hybrid search** — Qdrant (векторный) + полнотекстовый

## Приоритеты (что даст максимальный эффект)

1. 🔥 **Docling-парсинг** — сразу улучшит качество чанков для RAG
2. 🔥 **Auto-tagging** — автоматическая категоризация документов
3. ⭐ **Table extraction** — таблицы из PDF в структурированном виде
4. ⭐ **Object-level permissions** — гибкие права доступа
5. 📋 **Email ingestion** — автоматический забор почты
6. 📋 **VLM support** — графики/диаграммы/рукопись

## Технические риски

- Docling + VLM требует GPU для production (Ollama на 192.168.50.41 может помочь)
- PyTorch уже есть (Occular-ocr), конфликтов быть не должно
- GraniteDocling-258M — ~500MB, влезает в 15GB RAM сервера
