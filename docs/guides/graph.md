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

## ⚠️ Проблема с пустыми ответами LLM — ИСПРАВЛЕНА (2026-08-23)

**Симптом:** `Пустой ответ LLM для chunk_00001 (pass=extract):` для всех чанков; граф ~70 сек/документ впустую; Entity не создавались.

**Причина:** `function_map:graph` → `deepseek-v4-flash` — **reasoning-модель**. Сначала генерирует длинный `reasoning_content`, потом `content`. `max_tokens=800` (жёстко в `_call_llm`) — суммарный лимит, модель «думала» до исчерпания, content оставался пустым.

**Решение:** в `_call_llm` для deepseek/openai/openrouter добавлен `payload["thinking"] = {"type": "disabled"}` (коммит 4dcdc4d).

**Результат:** content_len=1211, reasoning=0; граф 25.4 сек вместо 71; сущности извлекаются.

**Диагностика (если снова пусто):** прямой вызов с полным промптом:
```python
# проверить reasoning_content и content
data = await resp.json()
msg = data['choices'][0]['message']
print(len(msg.get('content','')), len(msg.get('reasoning_content','')))
```

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
