# Гид: граф знаний (Neo4j)

> Актуально на 2026-08-23. ⚠️ ЕСТЬ ОТКРЫТАЯ ПРОБЛЕМА: LLM возвращает пустые ответы при извлечении сущностей.

## Текущее состояние

- **Neo4j Community** (не Enterprise): NODE KEY / composite constraints НЕ доступны → используем MERGE + отдельные индексы.
- Схема: `Document {id, filename}`, `Chunk {id, chunk_seq, text_preview}`, `Entity {name, type, source_docs}`.
- Связи: `(:Document)-[:HAS_CHUNK]->(:Chunk)`, `(:Chunk)-[:MENTIONS]->(:Entity)`, `(:Entity)-[:RELATED_TO]->(:Entity)`.
- Векторов в графе НЕТ — только текст-превью (первые ~500 символов).

## Как строится граф

`_build_knowledge_graph_async` в `src/api/services/document_service.py`:
1. Создаёт узел Document.
2. Обрабатывает **первые 10 чанков** (chunks[:10]):
   - создаёт узел Chunk (MERGE),
   - LLM-извлечение сущностей (`entity_extractor.extract_and_store`) — один промпт (core/relations/extended объединены, коммит df35877).

Таймауты (страховка от зависания):
- Neo4j-операция: 20 сек (to_thread + wait_for)
- Извлечение на чанк: 60 сек
- Весь граф: 300 сек — при превышении граф ПРОПУСКАЕТСЯ, документ завершается (граф вторичен).

## ⚠️ Открытая проблема: пустые ответы LLM

**Симптом:** `Пустой ответ LLM для chunk_00001 (pass=extract):` для всех 10 чанков.
Граф занимает ~70 сек на документ (10 × ~7 сек), но сущности НЕ извлекаются — в Neo4j только Document/Chunk, Entity нет.

**Причина:** `function_map:graph` → `deepseek-v4-flash` (api.deepseek.com). Модель не отдаёт JSON в формате промпта извлечения. Тот же класс проблемы, что в чате (flash молчит на больших промптах / не тот формат).

**Что НЕ является причиной:** сам Neo4j быстр (операции миллисекундные). Узкое место — последовательные внешние LLM-вызовы (~7 сек/чанк × 10).

**Кандидаты решения:**
1. Сменить модель для graph (локальная Ollama или более сильная модель).
2. Переписать промпт извлечения / парсинг ответа под flash.
3. Параллелить LLM-вызовы (asyncio.gather, лимит 2-3) — ускорит граф в 2-3 раза.
4. Пропускать граф при массовой переиндексации (экономия ~70 сек/документ).

## Диагностика

```bash
# Пустые ответы LLM
docker logs kag-worker | grep 'Пустой ответ LLM'
# Количество сущностей в Neo4j
docker exec kag-neo4j cypher-shell -u neo4j -p <пароль> \
  "MATCH (e:Entity) RETURN count(e);"
# Статистика графа
docker exec kag-neo4j cypher-shell -u neo4j -p <пароль> \
  "MATCH (d:Document) RETURN count(d), size((d)-[:HAS_CHUNK]->());"
# Логи графа
docker logs kag-worker | grep -E 'Граф знаний построен|Neo4j таймаут|Извлечение сущностей таймаут'
```

## Очистка при переиндексации

- `process_document(force=True)` вызывает `kg_service.clear_document(document_id)` ПЕРЕД переобработкой:
  - удаляет Document и его Chunk (DETACH DELETE),
  - убирает document_id из source_docs сущностей,
  - удаляет осиротевшие сущности (без MENTIONS).

## Полезные запросы Neo4j

```cypher
// Сущности по типу
MATCH (e:Entity) RETURN e.type, count(*) ORDER BY count(*) DESC LIMIT 20;
// Документы без чанков
MATCH (d:Document) WHERE NOT (d)-[:HAS_CHUNK]->() RETURN d.id, d.filename;
// Связи между сущностями
MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity) RETURN a.name, r.type, b.name LIMIT 50;
```
