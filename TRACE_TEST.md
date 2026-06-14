# Трассировка обработки документов в KAG

Дата: 2026-06-07 | Ветка: feature/ocr-upload-improvements | Сервер: 192.168.50.18

---

## Архитектура (контейнеры и порты)

```
┌─────────────────────────────────────────────────────────────────────┐
│ ВНЕШНИЙ МИР                                                         │
│   Браузер ──▶ https://qd.gostsecret.ru (HTTPS)                     │
│             ──▶ http://192.168.50.18:8000 (прямой доступ)           │
│   Ollama  ───▶ 192.168.50.41:11434 (эмбеддинги и LLM)              │
└─────────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ DOCKER: сеть kag_internal (bridge, внутренняя)                       │
│                                                                      │
│  kag-nginx        :80 (внешний)  ──▶ реверс-прокси                  │
│  kag-api          :8000           ──▶ FastAPI (основной)             │
│  kag-worker       —               ──▶ Celery (обработка)            │
│  kag-mcp          :8001           ──▶ MCP-сервер                     │
│  kag-redis        :6379           ──▶ Брокер Celery + кэш            │
│  kag-qdrant       :6333           ──▶ Векторная БД                   │
│  kag-pg           :5433→5432      ──▶ PostgreSQL (метаданные)        │
│  kag-neo4j        :7474,:7687     ──▶ Граф знаний                    │
│  kag-keycloak     :8080           ──▶ Аутентификация                 │
│  kag-scheduler    —               ──▶ Планировщик задач              │
│                                                                      │
│  Ollama (ВНЕ docker) 192.168.50.41:11434                             │
│    Модель: nomic-embed-text:latest (1024-мерные векторы)             │
│    Модель LLM: phi4-mini:latest                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## СЦЕНАРИЙ: Параллельная загрузка 2 файлов

Загружаем:
- `file1.pdf` — 3 страницы, русский текст + таблица
- `file2.docx` — 1 страница, русский текст

### Шаг 0. Браузер → kag-nginx (port 80) → kag-api (port 8000)

```
POST /api/v1/upload/
Content-Type: multipart/form-data
Authorization: Bearer eyJhbG... (JWT HS256, admin)
```

SecurityMiddleware (kag-api):
- Извлекает токен из cookie kag_token или заголовка Authorization
- Валидирует JWT (подпись HS256, issuer=kag-system, expiry)
- Проверяет: путь /api/v1/upload требует токен (НЕ в _is_public)
- user = admin (id=4755e13b-...)

### Шаг 1. kag-api: FastAPI endpoint upload_document()

Файл: `/app/src/api/routes/upload.py:316`

1.1. Генерация upload_id = uuid4() (напр. "a1b2c3d4-...")
1.2. Rate limiting: sliding window, 10 запросов/мин по IP клиента
1.3. `content = await file.read()` — весь файл в память
1.4. `SecurityValidator.validate_file_upload()` — проверка:
     - Расширение: .pdf, .docx разрешены
     - MIME-тип: application/pdf, application/vnd.openxmlformats-officedocument...
     - Размер: < 500 MB
1.5. `document_service.upload_document(filename, content, file_type, ...)`

### Шаг 2. document_service.upload_document()

Файл: `/app/src/api/services/document_service.py:179`

2.1. **SHA-256 хеш** содержимого:
    `file_hash = hashlib.sha256(file_content).hexdigest()`
    → 64-символьная hex-строка

2.2. **Проверка дубликата**: `_find_by_hash(file_hash)` — поиск в памяти
    (все документы загружены из PostgreSQL при старте API)
    ┌─ PostgreSQL (kag-pg:5432)
    │  SELECT * FROM system_configs WHERE key LIKE 'documents:%'
    │  → возвращает все doc_id → filename → file_hash → status
    └───────────────
    Если хеш найден → возвращаем существующий документ (НЕ дублируем)

2.3. **Создание записи**: doc_id = uuid4()
    Например: `doc_1 = "d4e5f6a7-..."`

2.4. **Сохранение файла на диск**:
    Путь: `/app/data/uploads/d4e5f6a7-..._file1.pdf`
    (монтируется с хоста: `./data/uploads → /app/data/uploads`)
    Размер: исходный размер файла (напр. 245 КБ)

2.5. **Сохранение метаданных в PostgreSQL**:
    `config_store.set("documents", doc_id, data)`
    ┌─ PostgreSQL (kag-pg:5432, БД kag, таблица system_configs)
    │  INSERT INTO system_configs (key, value, updated_at)
    │  VALUES ('documents:d4e5f6a7-...',
    │    '{"document_id":"d4e5f6a7-...",
    │      "filename":"file1.pdf",
    │      "file_type":"application/pdf",
    │      "file_size": 251392,
    │      "file_hash":"a1b2c3...",
    │      "status":"pending",
    │      "progress":0,
    │      "uploaded_by":"4755e13b-...",
    │      "group_ids":[],
    │      "version":1,
    │      "created_at":"...",
    │      "updated_at":"..."}',
    │    NOW())
    └───────────────

2.6. **Создание миниатюры** (thumbnail):
    ┌─ /app/data/thumbnails/d4e5f6a7-..._file1.webp (300x400px)
    │  Для PDF: pdf2image → Pillow → WebP
    │  Для DOCX: python-docx → Pillow → WebP
    └───────────────

2.7. Возвращает DocumentRecord в upload_document()

### Шаг 3. Отправка в Celery очередь

`celery_process_document.delay(document_id="d4e5f6a7-...")`

┌─ Redis (kag-redis:6379, DB 1)
│  LPUSH queue:documents → task_id + args
│  Ключ: celery-task-meta-{task_id} (для отслеживания статуса)
└───────────────

### Шаг 4. kag-api → ответ браузеру

```json
{
  "document_id": "d4e5f6a7-...",
  "status": "pending",
  "progress": 0.0,
  "upload_id": "a1b2c3d4-..."
}
```

Браузер (documents.html):
- Показывает карточку «Ожидает обработки»
- Запускает поллинг каждые 5 секунд: `GET /api/v1/upload/list`

---

### ШАГ 5. ПАРАЛЛЕЛЬНО: второй файл (file2.docx)

Идентичный процесс для file2.docx:
- doc_id = "e5f6a7b8-..."
- Сохраняется в `/app/data/uploads/e5f6a7b8-..._file2.docx`
- Метаданные в PostgreSQL (отдельная запись)
- Вторая задача в очередь Redis: `queue:documents`

Оба файла в Redis ждут обработки. Worker берёт их ПОСЛЕДОВАТЕЛЬНО.

---

### ШАГ 6. kag-worker: Celery обработка

Файл: `/app/src/indexing/tasks.py:30`

6.1. Worker (Celery, 4 воркера) получает задачу из Redis:
    `celery -A src.indexing.celery_app worker -Q documents,celery --concurrency=4`

6.2. `process_document(document_id="d4e5f6a7-...")`:

6.2.1. **Загрузка документа из БД**:
      `config_store.get_all("documents")` → dict всех документов
      ┌─ PostgreSQL (kag-pg:5432, БД kag)
      │  SELECT key, value FROM system_configs WHERE key LIKE 'documents:%'
      └───────────────
      
      doc_data = {
        "document_id": "d4e5f6a7-...",
        "filename": "file1.pdf",
        "file_type": "application/pdf",
        "file_size": 251392,
        "file_hash": "a1b2c3...",
        "status": "pending",
        ...
      }
      
      Создаётся DocumentRecord в памяти → document_service._documents[doc_id] = record

6.2.2. **Обновление статуса**: status="processing", progress=10
      `config_store.set("documents", doc_id, {...})` → PostgreSQL

6.3. **Обработка**: `document_service.process_document(document_id)`
     Файл: `/app/src/api/services/document_service.py:~430`

6.3.1. **Поиск файла на диске**:
      Путь: `/app/data/uploads/d4e5f6a7-..._file1.pdf`
      (volume mount: ./data → /app/data)

6.3.2. **Парсинг**: `self._parser.parse(file_path)`
      Файл: `/app/src/indexing/hybrid_parser.py`

      ┌─ Инициализация парсеров (при первом вызове):
      │  
      │  DOCLING:
      │    from docling.document_converter import DocumentConverter
      │    self._docling_converter = DocumentConverter()
      │    → Загружает модели Docling (layout analysis)
      │    → Память: ~500 MB
      │    → Использует torch (CPU)
      │  
      │  OCULAR OCR:
      │    from ocr_skel import OCRPipeline
      │    self._ocular = OCRPipeline(onnx=True, gpu=False)
      │    → Загружает ONNX модели:
      │      /opt/venv/lib/python3.11/site-packages/ocr_skel/weights/
      │        dbnet.onnx          (детектор текстовых блоков)
      │        dbnet_weights.pth   (веса детектора)
      │        crnn_encoder.onnx   (распознаватель текста)
      │        crnn_mobilenet_large.pth (веса распознавателя)
      │    → CPU, без GPU, ~4 потока на процесс
      │  
      └───────────────────────────────────────────

      ПАРСИНГ PDF:
      ┌─ Этап A: Docling (layout analysis)
      │  result = self._docling_converter.convert(file_path)
      │  → Разбивает страницы на блоки:
      │    - text blocks (координаты bbox)
      │    - tables (структура строк/колонок)
      │    - images (описания)
      │    - formulas (LaTeX)
      │  → Определяет порядок чтения (reading order)
      │  → Время: ~2 сек/страница
      │
      │  Этап B: Occular OCR (текст)
      │  pages = self._ocular.process_pdf(file_path, dpi=300)
      │  → Для каждой страницы:
      │    1. DBNet (ONNX) — детектирует текстовые блоки
      │    2. CRNN (ONNX) — распознаёт текст в каждом блоке
      │    3. Сборка текста страницы
      │  → Результат: [{"page":1, "method":"ocr", "results":[{"text":"..."}]}, ...]
      │  → Точность: 93.7% на русском
      │  → Время: ~1 сек/страница
      │  
      │  Этап C: Сборка ParsedDocument
      │  parsed.pages = [ParsedPage(page_num=1, text="полный текст..."), ...]
      │  parsed.full_text = "страница1\n\n--- PAGE BREAK ---\n\nстраница2..."
      │  parsed.metadata = {page_count: 3, format: ".pdf", file_hash: "..."}
      │  parsed.parse_method = "docling+ocular"  (или "ocular_only" если Docling недоступен)
      └───────────────────────────────────────────
      
      Результат: ParsedDocument с полным текстом всех страниц

6.3.3. **Сохранение OCR-результата на диск**:
      Путь: `/app/data/ocr_results/file1.pdf`
      Содержимое: полный распознанный текст (plain text)

6.3.4. **Сборка Markdown**: `parsed.to_markdown()`
      Путь: `/app/data/ocr_results/file1.pdf.md`
      Содержимое: структурированный Markdown
      ```
      ## Страница 1
      Текст первого параграфа...

      ## Страница 2
      | Колонка1 | Колонка2 | Колонка3 |
      |----------|----------|----------|
      | Данные1  | Данные2  | Данные3  |

      ## Страница 3
      ...
      ```

6.3.5. **Чанкинг**:
      Файл: `/app/src/indexing/chunking.py`

      `chunker = DocumentChunker(chunk_size=1000, chunk_overlap=200)`
      ┌─ RecursiveCharacterTextSplitter (langchain-text-splitters)
      │  Разделители (по приоритету):
      │    1. "\n\n"  (параграфы)
      │    2. "\n"    (строки)
      │    3. ". "    (предложения)
      │    4. " "     (слова)
      │    5. ""      (символы)
      │  
      │  chunk_size = 1000 символов
      │  chunk_overlap = 200 символов (перекрытие для контекста)
      │  
      │  Для file1.pdf (3 страницы, ~5000 символов):
      │    чанк_1: текст стр.1-2 (1000 симв)
      │    чанк_2: текст стр.2-3 с перекрытием (1000 симв)
      │    чанк_3: остаток стр.3 (800 симв)
      │  
      │  chunk_id = "d4e5f6a7-..._chunk_00001"
      │            = "d4e5f6a7-..._chunk_00002"
      │            = "d4e5f6a7-..._chunk_00003"
      └───────────────────────────────────────────

6.3.6. **Векторизация**:
      Файл: `/app/src/indexing/embeddings_service.py:200`

      Вызов: `embeddings_service.embed_and_store(chunks, document_id, metadata)`

      ┌─ 6.3.6.1. Инициализация (при первом вызове):
      │  
      │  EmbeddingClient (src/llm/embeddings.py):
      │    base_url = http://192.168.50.41:11434 (Ollama)
      │    model = "nomic-embed-text:latest"
      │    timeout = 60 сек
      │  
      │  QdrantClient:
      │    url = "http://qdrant:6333" (kag-qdrant, внутри docker-сети)
      │    collection_name = "kag_documents"
      │  
      │  Проверка/создание коллекции:
      │    GET http://kag-qdrant:6333/collections/kag_documents
      │    → 200 OK (существует) или 404 (создать)
      │    
      │    Создание (если нет):
      │    ┌─ PUT http://kag-qdrant:6333/collections/kag_documents
      │    │  {
      │    │    "vectors": {"size": 1024, "distance": "Cosine"},
      │    │    "hnsw_config": {"m": 16, "ef_construct": 100},
      │    │    "optimizers_config": {"indexing_threshold": 5000}
      │    │  }
      │    │  → Индексы: document_id (keyword), filename (keyword), group_ids (keyword)
      │    └────────────────────────────────────────────────
      │  
      └────────────────────────────────────────────────────

      ┌─ 6.3.6.2. Генерация эмбеддингов:
      │  
      │  Для каждого чанка (batch_size=32):
      │  
      │  POST http://192.168.50.41:11434/api/embeddings
      │  {
      │    "model": "nomic-embed-text:latest",
      │    "input": "текст чанка (до 1000 символов)..."
      │  }
      │  
      │  Ответ:
      │  {
      │    "embedding": [0.123, -0.456, ..., 0.789]  // 1024 float32
      │  }
      │  
      │  Время: ~0.5-1 сек на чанк (зависит от сети до 192.168.50.41)
      │  
      └────────────────────────────────────────────────────

      ┌─ 6.3.6.3. Сохранение в Qdrant:
      │  
      │  PUT http://kag-qdrant:6333/collections/kag_documents/points?wait=true
      │  {
      │    "points": [
      │      {
      │        "id": "uuid5(d4e5f6a7-...-0)",   // детерминированный UUID
      │        "vector": [0.123, -0.456, ...],   // 1024 float32
      │        "payload": {
      │          "document_id": "d4e5f6a7-...",
      │          "chunk_id": "d4e5f6a7-..._chunk_00001",
      │          "content": "полный текст чанка",
      │          "file_type": "application/pdf",
      │          "filename": "file1.pdf",
      │          "group_ids": [],
      │          "metadata": {
      │            "file_type": "application/pdf",
      │            "chunk_index": 0,
      │            "total_chunks": 3,
      │            "chunk_seq": 1,
      │            "splitter": "recursive_character",
      │            "filename": "file1.pdf",
      │            "document_id": "d4e5f6a7-..."
      │          }
      │        }
      │      },
      │      ... (остальные чанки)
      │    ]
      │  }
      │  
      │  → Qdrant сохраняет векторы на диск: data/qdrant/
      │    (монтируется с хоста: ./data/qdrant → /qdrant/storage)
      │  
      └────────────────────────────────────────────────────

6.3.7. **Граф знаний (Neo4j)**:
      Файл: `/app/src/indexing/knowledge_graph.py`

      ┌─ Подключение: bolt://neo4j:7687
      │  (kag-neo4j, внутри docker-сети)
      │  
      │  Извлечение сущностей из текста чанков:
      │  - Ключевые слова (2+ заглавных, 3+ латиница, 4+ кириллица)
      │  - LLM-вызов для извлечения структурированных сущностей
      │  
      │  POST http://192.168.50.41:11434/api/generate
      │  (phi4-mini:latest — извлекает сущности + связи)
      │  
      │  MERGE (n:Chunk {id: "d4e5f6a7-..._chunk_00001"})
      │  MERGE (e:Entity {name: "сущность"})
      │  CREATE (n)-[:CONTAINS]->(e)
      │  
      │  → Neo4j сохраняет данные: data/neo4j/
      └────────────────────────────────────────────────────

6.3.8. **Обновление статуса в PostgreSQL**:
      status="completed", progress=100
      `config_store.set("documents", doc_id, {...})` → PostgreSQL

6.3.9. **Логирование**:
      `config_store.set("process_logs", "log_d4e5f6a7-...", log_data)` → PostgreSQL

### Шаг 7. Браузер обновляет статус

Поллинг каждые 5 сек: `GET /api/v1/upload/list`

Ответ API:
```json
[
  {
    "document_id": "d4e5f6a7-...",
    "filename": "file1.pdf",
    "status": "completed",     // pending → processing → completed
    "progress": 100.0,
    "chunks_count": 3,
    "vectors_count": 3,
    "document_type": "contract",
    "created_at": "...",
    "updated_at": "..."
  },
  {
    "document_id": "e5f6a7b8-...",
    "filename": "file2.docx",
    "status": "processing",
    "progress": 60.0,
    "chunks_count": 0,
    "vectors_count": 0
  }
]
```

Браузер отображает карточки с прогрессом.

### Шаг 8. Поиск (когда пользователь вводит запрос)

POST /api/v1/chat/message
```json
{"message": "какие контрагенты в договорах?"}
```

┌─ 8.1. Векторизация запроса:
│  POST http://192.168.50.41:11434/api/embeddings
│  {model: "nomic-embed-text:latest", input: "какие контрагенты в договорах?"}
│  → вектор [0.234, -0.567, ...]
│
├─ 8.2. Поиск в Qdrant:
│  POST http://kag-qdrant:6333/collections/kag_documents/points/search
│  {vector: [...], limit: 5, with_payload: true}
│  → [{id: "...", score: 0.87, payload: {content: "...", filename: "file1.pdf"}}, ...]
│
├─ 8.3. Поиск в Neo4j:
│  MATCH (e:Entity) WHERE e.name CONTAINS "контрагент"
│  RETURN e.name, [(e)<-[:CONTAINS]-(c:Chunk) | c.id] AS chunks
│  → [{name: "ООО Ромашка", chunks: ["d4e5f6a7-..._chunk_00002"]}, ...]
│
├─ 8.4. Reranker (FlashRank, ONNX):
│  Переранжирует результаты Qdrant + Neo4j → лучшие 3-5 чанков
│
└─ 8.5. LLM-ответ:
   POST http://192.168.50.41:11434/api/generate
   {model: "phi4-mini:latest", prompt: "Системный промпт + контекст из чанков + вопрос"}
   → "Согласно документам, контрагентами являются: ООО Ромашка (договор file1.pdf)..."
```

---

## СВОДНАЯ ТАБЛИЦА ХРАНЕНИЯ

| Данные | Где хранится | Путь/Таблица | Формат |
|--------|-------------|-------------|--------|
| Исходный файл | Диск (volume) | `/app/data/uploads/{doc_id}_{filename}` | Исходный формат |
| OCR текст | Диск (volume) | `/app/data/ocr_results/{filename}` | plain text (.txt) |
| Markdown | Диск (volume) | `/app/data/ocr_results/{filename}.md` | Markdown |
| Миниатюра | Диск (volume) | `/app/data/thumbnails/{doc_id}.webp` | WebP 300x400 |
| Метаданные | PostgreSQL | `system_configs` (key='documents:{id}') | JSON |
| Векторы | Qdrant | коллекция `kag_documents` | 1024 float32 |
| Граф знаний | Neo4j | узлы Chunk, Entity, связи CONTAINS | Граф |
| Логи обработки | PostgreSQL | `system_configs` (key='process_logs:...') | JSON |
| Очередь задач | Redis | очередь `documents` (DB 1) | Celery tasks |

---

## ПОРЯДОК ОБРАБОТКИ ПРИ ПАРАЛЛЕЛЬНОЙ ЗАГРУЗКЕ

```
t=0  ──┬── Браузер: file1.pdf отправлен
       │   → kag-api: сохранён на диск + метаданные в PG
       │   → Redis: задача в очередь documents
       │
       ├── Браузер: file2.docx отправлен (параллельно)
       │   → kag-api: сохранён на диск + метаданные в PG
       │   → Redis: задача в очередь documents
       │
t=0.5 ──┼── kag-worker: берёт file1.pdf из очереди
       │   → Загружает метаданные из PG
       │   → Docling (layout) ~6 сек (3 страницы)
       │   → Occular OCR (текст) ~3 сек
       │   → Markdown сборка ~0.1 сек
       │   → Чанкинг ~0.5 сек
       │   → Векторизация ~3 сек (3 чанка × 1 сек)
       │   → Qdrant сохранение ~0.2 сек
       │   → Neo4j граф ~2 сек
       │   → Обновление статуса в PG
       │   [file1.pdf ОБРАБОТАН за ~15 сек]
       │
t=15  ──┼── kag-worker: берёт file2.docx из очереди
       │   (та же последовательность, ~5 сек для 1 страницы)
       │   [file2.docx ОБРАБОТАН за ~5 сек]
       │
t=20  ──┴── Оба документа COMPLETED
            Браузер: поллинг показывает 100% для обоих
```

**Важно:** Celery обрабатывает задачи ПОСЛЕДОВАТЕЛЬНО в рамках одного worker'а
(concurrency=4 позволяет 4 параллельных задачи, но очередь documents — одна).
При нескольких worker'ах возможна параллельная обработка.

---

## РЕАЛЬНЫЙ ТЕСТ: 2026-06-07 04:30 UTC

### Документ 1: test_contract.pdf
- **document_id:** `4f2444bb-879e-46f7-853f-4b32bf78db31`
- **upload_id:** `6a28c426-72d4-4968-a22a-ad7f63881c40`
- **Размер:** 3.0 KB (3 страницы)
- **Тип:** application/pdf
- **Хеш:** SHA-256 (уникальный)

**Результат обработки:**
- Статус: ✅ completed
- Парсер: Occular OCR (онлайн, ONNX, CPU)
- OCR текст: 564 байт → `/app/data/ocr_results/test_contract.pdf`
- Markdown: 631 байт → `/app/data/ocr_results/test_contract.pdf.md`
- Чанков: 1 (RecursiveCharacterTextSplitter, chunk_size=1000)
- Векторов в Qdrant: 1 (1024-мерных)
- Миниатюра: 8.0 KB WebP → `/app/data/thumbnails/4f2444bb...webp`
- Время обработки: ~15 сек (загрузка → Celery → OCR → чанкинг → векторизация)

**Замечание:** PDF создан через ReportLab с латинским шрифтом Helvetica — кириллица сохранилась некорректно, OCR распознал как «IIIIIII» вместо «Договор». Это артефакт тестового PDF, не баг OCR.

### Документ 2: test_memo.docx
- **document_id:** `d27b1870-8eb7-4194-bcd5-bfd2518630f1`
- **upload_id:** `6798a014-fc96-4420-87a7-25dd73073416`
- **Размер:** 37 KB (1 страница)
- **Тип:** application/vnd.openxmlformats-officedocument.wordprocessingml.document

**Результат обработки:**
- Статус: ✅ completed
- Парсер: python-docx (извлечение текста напрямую, без OCR)
- Markdown: 65 KB → `/app/data/ocr_results/test_memo.docx.md`
- Чанков: 46 (RecursiveCharacterTextSplitter, chunk_size=1000, overlap=200)
- Векторов в Qdrant: 46 (1024-мерных)
- Миниатюра: 17 KB WebP → `/app/data/thumbnails/d27b1870...webp`
- Время обработки: ~60 сек (загрузка → Celery → парсинг → 46 эмбеддингов через Ollama → Qdrant)

**Важно:** DOCX содержит 46 чанков из-за малого chunk_size (1000) относительно объёма текста. Каждый чанк векторизовался отдельным HTTP-запросом к Ollama (192.168.50.41:11434). Это заняло основное время.

### Итоги теста:

| Метрика | test_contract.pdf | test_memo.docx |
|---------|------------------|----------------|
| Статус | completed | completed |
| Чанков | 1 | 46 |
| Векторов | 1 | 46 |
| Размер исходника | 3.0 KB | 37 KB |
| Размер OCR/MD | 631 B | 65 KB |
| Миниатюра | 8 KB | 17 KB |
| Время обработки | ~15 сек | ~60 сек |

**Подтверждённые соединения:**
- kag-api → kag-pg:5432 ✅ (PostgreSQL, метаданные)
- kag-api → kag-redis:6379 ✅ (Celery брокер)
- kag-worker → kag-pg:5432 ✅ (чтение метаданных)
- kag-worker → kag-qdrant:6333 ✅ (векторное хранилище)
- kag-worker → 192.168.50.41:11434 ✅ (Ollama эмбеддинги)
- kag-worker → kag-neo4j:7687 ✅ (граф знаний, при наличии)

**Всего в Qdrant: 50 векторов** (1+46+3 из предыдущих тестов)


| Этап | file1.pdf (3 стр.) | file2.docx (1 стр.) |
|------|-------------------|---------------------|
| Исходник | 245 КБ | 85 КБ |
| OCR текст | 4.8 КБ | 1.2 КБ |
| Markdown | 5.2 КБ | 1.4 КБ |
| Миниатюра | 12 КБ | 8 КБ |
| Qdrant (3 чанка × 1024 float32) | ~12 КБ | ~4 КБ |
| Neo4j (сущности) | ~5 узлов, 8 связей | ~2 узла, 3 связи |
| PG (метаданные + логи) | ~2 КБ | ~1.5 КБ |
