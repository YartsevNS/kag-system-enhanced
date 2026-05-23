# Поток данных в Neo4j (Граф знаний)

## Общая схема
Данные попадают в Neo4j **асинхронно** после загрузки и векторизации документа.

`Загрузка файла` → `Парсинг (Hybrid)` → `Чанкинг` → `Векторизация (Qdrant)` → `Построение графа (Neo4j)`

---

## Детальный разбор

### Шаг 1: Загрузка и обработка
1. **Загрузка**: Через UI (`/documents`) или API (`POST /api/v1/upload`).
2. **Парсинг**: `src/indexing/tasks.py` вызывает `process_document`.
   - Используется `hybrid_parser` (Docling + Occular-ocr).
3. **Чанкинг**: `DocumentChunker` разбивает текст на части.
4. **Векторизация**: `Vectorizer` сохраняет чанки в Qdrant (через Celery задачу `vectorize_document`).

### Шаг 2: Построение графа (Neo4j)
Выполняется в фоне функцией `_build_knowledge_graph_async` в `src/api/services/document_service.py`.

**Местоположение**: `document_service.py`, строка ~466.

1. **Создание узла документа**:
   ```python
   kg_service.create_document_node(document_id, filename)
   ```
   - Создает узел `(:Document {id, filename})`.

2. **Обработка чанков** (первые 10 для скорости):
   - Для каждого чанка создается узел и связь с документом:
     ```python
     kg_service.create_chunk_node(chunk_id, document_id, text, chunk_seq)
     ```
     - Создает `(:Chunk {id, text, chunk_seq})`.
     - Создает связь `(:Document)-[:HAS_CHUNK]->(:Chunk)`.

3. **Извлечение сущностей (Entity Extraction)**:
   - Вызывается `entity_extractor.extract_and_store(...)`.
   - **Локация**: `src/indexing/entity_extractor.py`, строка ~118.
   - Текст чанка отправляется в LLM (Ollama/Mistral) для извлечения:
     - Сущности: `person`, `organization`, `date`, `money`, `location`.
     - Связи: `MENTIONS`, `SIGNED_BY`, `BELONGS_TO` и др.

4. **Сохранение сущностей в Neo4j**:
   - `kg_service.create_entity(entity)`:
     - `MERGE (e:Entity {name, type})`.
     - `MERGE (c:Chunk)-[:MENTIONS]->(e)`.
   - `kg_service.create_relation(relation)`:
     - Создает связи между сущностями.

---

## Реализация в коде

### Файлы:
- **`src/indexing/knowledge_graph.py`**: Сервис `KnowledgeGraphService`. Методы:
  - `create_document_node()`
  - `create_chunk_node()`
  - `create_entity()`
  - `create_relation()`
  - `search_entities()`
  - `hybrid_search()` (граф + вектор).

- **`src/indexing/entity_extractor.py`**: Класс `EntityExtractor`.
  - Метод `extract_and_store()`: отправляет чанк в LLM и сохраняет результат в граф.

- **`src/api/services/document_service.py`**:
  - Метод `_build_knowledge_graph_async()`: orchestrator построения графа.

### API Эндпоинты:
- `GET /api/v1/kg/stats` — статистика (документы, чанки, сущности).
- `GET /api/v1/kg/entities/search?q=...` — поиск сущностей.
- `GET /api/v1/kg/hybrid-search?q=...` — гибридный поиск (граф + вектор).

---

## Почему гибридный поиск может не работать?

1. **Граф пуст (entities: 0)**:
   - Не выполнилась задача `_build_knowledge_graph_async`.
   - LLM (Ollama) недоступен или не отвечает.
   - Ошибка в `entity_extractor.py` (промпт, парсинг JSON).

2. **Ошибка авторизации (401/403)**:
   - Исправлено в `kg.html`: добавлены `getToken()` и `authHeaders()`.

3. **Ошибка `d.results is undefined`**:
   - Исправлено добавлением проверки `Array.isArray(d.results)` и обработкой не-200 статусов.

---

## Как проверить статус?

1. **Статистика графа**:
   ```bash
   curl -s -u "admin:KAGadmin2026!secure" "http://192.168.50.18:8000/api/v1/kg/stats"
   ```

2. **Проверка Neo4j (через браузер или cypher)**:
   - URL: http://192.168.50.18:7474
   - Логин: `neo4j`, Пароль: `kagneo4j2026`
   - Запрос: `MATCH (n) RETURN n LIMIT 25`

3. **Логи API**:
   ```bash
   docker-compose logs --tail=50 api | grep -i "graph\|entity\|neo4j"
   ```

4. **Проверка Ollama (LLM)**:
   ```bash
   curl -s http://192.168.50.41:11434/api/tags
   ```
