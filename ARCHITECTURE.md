# KAG — Архитектура распознавания документов (v3)

> Последнее обновление: 31 мая 2026
> Ветка: `feature/ocr-upload-improvements`

## Общая схема обработки документа

```
┌─────────────────────────────────────────────────────────────────┐
│                         ЗАГРУЗКА                                │
│  Браузер → POST /api/v1/upload/ → сохранить на диск            │
│  → document_service.upload_document()                           │
│  → config_store (PostgreSQL): метаданные                        │
│  → Celery: process_document.delay(document_id)                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      РАСПОЗНАВАНИЕ (Celery Worker)              │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ 1. LAYOUT ANALYSIS (Docling)                             │  │
│  │    DocumentConverter → где таблицы, заголовки, колонки    │  │
│  │    Результат: layout_map {page, items: [table, text, ..]} │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ 2. TEXT EXTRACTION (Occular OCR)                         │  │
│  │    OCRPipeline(onnx=True, gpu=False)                     │  │
│  │    → Русский текст (93.7% accuracy)                      │  │
│  │    → ONNX runtime, 4 потока CPU, без GPU                 │  │
│  │    Результат: full_text (строка), pages[{page, text}]    │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ 3. TABLE EXTRACTION (Granite-Docling 258M / docling)     │  │
│  │    Granite-Docling 258M: image → table Markdown            │  │
│  │    или docling built-in: extract_table_structure()        │  │
│  │    Результат: tables[{markdown, cells, html}]             │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ 4. MARKDOWN ASSEMBLY                                     │  │
│  │    Layout + Occular text + Granite tables → Markdown      │  │
│  │    Сохраняется: /app/data/ocr_results/{filename}.md       │  │
│  └───────────────────────────────────────────────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ 5. CHUNKING + VECTORIZATION                              │  │
│  │    RecursiveCharacterTextSplitter                         │  │
│  │    → chunks → Qdrant (векторы + текст чанков)            │  │
│  │    → Neo4j (граф знаний)                                 │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Где что хранится

| Данные | Хранилище | Формат |
|--------|-----------|--------|
| Исходный файл | `/app/data/uploads/{doc_id}_{filename}` | PDF/DOCX/исходный |
| Распознанный текст (полный) | `/app/data/ocr_results/{filename}` | TXT (UTF-8) |
| Markdown с таблицами | `/app/data/ocr_results/{filename}.md` | Markdown |
| Метаданные | PostgreSQL (config_store → kag-pg) | JSON |
| Векторы + текст чанков | Qdrant (kag_documents) | Вектор 1024 + payload |
| Сущности + связи | Neo4j | Граф |

## Occular OCR — русский текст

**Модель**: DBNet (детекция текста) + CRNN (распознавание)
**Пакет**: `ocr_skel` (из репозитория Occular-ocr)
**Установка**: `pip install git+https://github.com/Bodhi42/Occular-ocr.git`
**Веса**: 
- `dbnet.onnx` (детекция)
- `crnn_encoder.onnx` (распознавание, словарь 298 символов)
- `dbnet_weights.pth` (веса PyTorch → ONNX)
- `crnn_mobilenet_large.pth`

**Инициализация**: `OCRPipeline(onnx=True, gpu=False)`
**Потоки**: onnxruntime сам определяет (обычно все ядра CPU, 4 на нашем сервере)
**Точность**: 93.7% на русском тексте

## Docling — анализ макета

**Библиотека**: `docling-core` (ядро, без torch)
**Класс**: `DocumentConverter` → `convert(file_path)` → `DoclingDocument`
**Что даёт**:
- Разбиение на страницы
- Определение типов элементов: `text`, `table`, `image`, `heading`
- Координаты элементов на странице
- Порядок чтения

**Без Granite**: базовое определение таблиц (координаты ячеек)
**С Granite**: точная структура таблиц + Markdown

## Granite-Docling 258M — таблицы

**Модель**: `ibm-granite/granite-docling-258M` (258M параметров)
**Тип**: image-to-text (изображение → Markdown с разметкой)
**Лицензия**: Apache 2.0
**Требования**: transformers, torch (≈500MB для модели + зависимости)

**Когда применять**:
- Документы с таблицами
- Требуется точная структура таблиц (ячейки, строки, colspan/rowspan)

**Ограничения**:
- Тренирована на английских документах
- Для русского текста в таблицах — комбинировать с Occular
- Требует torch (≈2GB зависимостей)

## Сборка Markdown

После получения layout (Docling), текста (Occular) и таблиц (Granite/docling):

```python
def assemble_markdown(layout, ocr_text, tables):
    md = []
    for item in layout:
        if item.type == 'heading':
            md.append(f"{'#' * item.level} {ocr_text[item.region]}")
        elif item.type == 'text':
            md.append(ocr_text[item.region])
        elif item.type == 'table':
            md.append(tables[item.table_id].markdown)
        elif item.type == 'image':
            md.append(f"![{item.caption}]({item.image_path})")
    return "\n\n".join(md)
```

Результат сохраняется в `/app/data/ocr_results/{filename}.md`

## Fallback цепочка

```
1. Docling + Occular (layout + Russian text) ← приоритет
2. Occular only (только текст, без структуры)
3. DocumentParser (pypdf/tesseract) ← крайний fallback
```

Granite-Docling 258M — опциональное улучшение для таблиц.
Если torch недоступен → используем docling built-in table extraction.

## Чанкинг

**Библиотека**: `langchain-text-splitters` (RecursiveCharacterTextSplitter)
**Параметры**: `chunk_size=1000`, `chunk_overlap=200`
**Разделители**: `\n\n` → `\n` → `. ` → ` ` → ``

## Векторизация

**Модель**: `qllama/bge-m3:Q4_K_M` (через Ollama на 192.168.50.41:11434)
**Размерность**: 1024
**Хранилище**: Qdrant (коллекция `kag_documents`)

## Текущие проблемы и решения

| Проблема | Решение |
|----------|---------|
| Worker не видит новые документы | Celery задача загружает из config_store перед обработкой |
| Две БД (keycloak-db vs kag-pg) | KAG_DB_URL унифицирует (оба на kag-pg) |
| torch тянет 2GB | Убран из зависимостей (Occular на ONNX) |
| Миниатюры не создавались | chown /app/data, mkdir thumbnails |
| Redis терял сеть | docker-compose --force-recreate |
| Документы в pending без обработки | Исправлено: KAG_DB_URL + загрузка из БД в worker |
| **Worker не имел Occular/docling** | Dockerfile.worker обновлён: добавлены Tesseract, Occular, веса |

## Изменения Dockerfile.worker (31 мая 2026)

**Проблема**: Worker собирался ТОЛЬКО из requirements.txt (без Occular OCR, без Docling).
При обработке падал `Docling not available: No module named 'docling'` и `Occular-ocr not available: No module named 'ocr_skel'`.
Обработка шла через DocumentParser (pypdf/tesseract) — **без русского OCR**.

**Исправление**: Dockerfile.worker теперь включает:
```dockerfile
# Stage 1 (builder)
RUN apt-get install -y tesseract-ocr tesseract-ocr-rus poppler-utils libgl1 libglib2.0-0t64

RUN pip install git+https://github.com/Bodhi42/Occular-ocr.git
RUN curl -sLO ...dbnet.onnx ...crnn_encoder.onnx ...crnn_mobilenet_large.pth

# Stage 2 (production)
RUN apt-get install -y tesseract-ocr tesseract-ocr-rus poppler-utils libgl1 libglib2.0-0t64
```

Теперь Worker имеет те же OCR-возможности, что и API.

---

## Веб-мониторинг и загрузка документов (v1)

> Добавлено: 8 июня 2026


---

## Веб-мониторинг и загрузка документов (v1)

> Добавлено: 8 июня 2026

### Источники
Поддерживаются три типа источников:
- **RSS/Atom** — парсинг лент (pravo.gov.ru, cbr.ru)
- **Scrape** — извлечение ссылок со страниц (gost.ru — 116 файлов)
- **Change Detection** — отслеживание изменений по SHA-256

### Процесс скачивания
1. Проверка источника -> извлечение URL документов
2. Фильтрация: `/file-service/file/load/`, `.pdf`, `.docx`
3. Дедупликация: `_seen_urls` (в памяти) + SHA-256 (по содержимому)
4. Скачивание партиями (batch_size=5, пауза 15 сек между партиями)
5. Каждый файл -> `document_service.upload_document()` -> Celery `process_document`
6. OCR -> Markdown -> чанки -> Qdrant + Neo4j

### Счётчики
- `items_found` — найдено ссылок в последнюю проверку
- `items_uploaded` — всего скачано (кумулятивно)
- `download_attempts` — попыток скачивания (downloaded/skipped/duplicate/error)

### Хранение метаданных
- `config_store("web_monitor", "sources")` — источники
- `config_store("web_monitor", "downloads")` — история скачиваний
- `config_store("web_monitor", "history")` — история проверок

### Важно
- При скачивании 100+ файлов процесс идёт в фоне (async)
- Не обрывать соединение во время загрузки
- Дедупликация по URL + SHA-256 предотвращает повторную загрузку
- SSL verify отключен для гос.порталов (aiohttp.TCPConnector(ssl=False))

---

## Оптимизация OCR: Docling + Occular (v2)

> Добавлено: 8 июня 2026

### Сравнение
| Компонент | Назначение | Скорость | Когда использовать |
|-----------|-----------|----------|-------------------|
| Docling | Извлечение текста из PDF + layout analysis | 1-2 сек/стр | Текстовые PDF (ГОСТы, приказы) |
| Occular OCR | Распознавание текста с изображений | 10-15 сек/стр | Сканы, фото, PDF без текстового слоя |

### Логика пропуска OCR
- Docling успешно извлёк >100 символов текста -> пропускаем Occular
- Текст содержит артефакты (□□□, ???) -> запускаем Occular
- force_ocr=True -> всегда запускаем Occular

### Настройки (админ-панель)
- **force_ocr**: принудительный OCR даже для текстовых PDF
- **dpi**: качество рендеринга (100-400, по умолчанию 200)

### Вывод
Occular НЕ может заменить Docling — он только распознаёт текст с изображений.
Docling даёт layout analysis (таблицы, заголовки, колонки).
Они дополняют друг друга: Docling для структуры, Occular для OCR.
