# Docling — Анализ проекта

**GitHub:** docling-project/docling | ⭐ 59,677 | Python | IBM-backed  
**arXiv:** 2408.09869 | **Лицензия:** MIT

## Что такое Docling

Универсальный конвертер документов для gen AI. Парсит PDF, DOCX, PPTX, XLSX, HTML, изображения, аудио и ещё 10+ форматов в структурированный `DoclingDocument`. Проект уровня enterprise от IBM Research.

## Ключевые возможности

### 1. Универсальный парсинг
- **PDF:** layout analysis (Heron model), reading order, table extraction, formulas, code blocks, image classification
- **Office:** DOCX, PPTX, XLSX с сохранением структуры
- **Web:** HTML, Markdown, LaTeX, plain text
- **Медиа:** PNG, TIFF, JPEG, WAV, MP3 (ASR), WebVTT
- **Специализированные:** USPTO патенты, JATS статьи, XBRL финансы

### 2. Визуальные языковые модели (VLM)
- GraniteDocling-258M — собственная модель IBM для понимания документов
- Поддержка любых VLM через pluggable архитектуру
- Chart understanding: bar/pie/line charts → таблицы/код

### 3. Экспортные форматы
- Markdown, HTML, WebVTT, DocTags (lossless JSON для обучения)
- LangChain, LlamaIndex, Crew AI, Haystack — готовые интеграции
- MCP server для agentic AI

### 4. Производительность
- Локальное исполнение (air-gapped)
- GPU и CPU режимы
- Новая модель Heron — быстрее парсинг PDF

## Архитектура

```
Input (PDF/DOCX/IMG/...) 
  → DocumentConverter 
  → Pipeline (layout → table → formula → OCR) 
  → DoclingDocument (unified representation) 
  → Export (MD/HTML/JSON/DocTags)
```

Три уровня пайплайна:
1. **Standard** — базовый парсинг (быстрый)
2. **VLM** — визуальные модели (точный, для сложных документов)
3. **Hybrid** — комбинация (баланс)

## Docling-serve (API сервер)

REST API для Docling:
- `POST /v1/convert` — конвертировать документ (file или URL)
- Поддержка асинхронных задач
- WebSocket для стриминга прогресса
- MCP endpoint для агентов
- Docker-деплой из коробки

## Интеграция с kag-system

### Что можно взять:
1. **Заменить текущий парсинг PDF/DOCX** — Docling даёт на порядок лучше качество: таблицы, формулы, reading order
2. **VLM для сложных документов** — GraniteDocling для сканов с графиками
3. **MCP server** — подключить kag-system как MCP-клиент к Docling
4. **Структурированное извлечение** — метаданные (title, authors, references)

### Сложности:
- Тяжёлые модели (нужен GPU для VLM)
- Зависимость от PyTorch (у нас уже есть)
- Heron модель ~500MB

## Вердикт

**Обязательно интегрировать.** Замена текущего парсера на Docling даст качественный скачок в обработке документов. Особенно ценны: table extraction, reading order, VLM support.
