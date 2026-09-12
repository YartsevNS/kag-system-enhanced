# Гид: структурные поля payload и единый идентификатор чанка

> Введено 2026-09-12. Меняет схему payload Qdrant и узла Chunk в Neo4j.
> Для применения к существующим документам нужна переиндексация.

## 1. Единый идентификатор (ID)

| Сущность | Идентификатор | Как считается |
|---|---|---|
| Чанк в Neo4j | `Chunk.id` | `{document_id}_chunk_{seq:05d}` (человекочитаемый) |
| Точка в Qdrant | `point_id` | `uuid5(NAMESPACE_DNS, "kag-chunk:{document_id}:{chunk_id}")` |
| Обратная ссылка | `Chunk.qdrant_point_id` | тот же uuid5 (пишется при создании узла) |

Зачем: из графа можно вычислить `point_id` и достать вектор точечно (без поиска
по payload). Повторная индексация того же `chunk_id` даёт тот же `point_id`.

⚠️ **document_id в ключе обязателен.** `chunk_id` вида `chunk_00001` встречается
в КАЖДОМ документе — без `document_id` все документы писали бы точки с одним
`point_id` и перезаписывали друг друга (симптом: документ «обработан, 180 чанков»,
а в Qdrant по нему 0 точек). Смежное требование: чанкинг обязан получать
`document_id` (`parsers.chunk_document(segments, document_id)`), иначе `chunk_id`
теряет префикс документа. Проверка после индексации:

```bash
curl -s -X POST http://localhost:6333/collections/kag_documents/points/count \
  -H "api-key: $QDRANT_API_KEY" -H 'Content-Type: application/json' \
  -d '{"exact": true, "filter": {"must": [{"key":"document_id","match":{"value":"<id>"}}]}}'
# count должен быть > 0
```

```python
from src.indexing.ids import point_id_for_chunk
point_id_for_chunk("doc123_chunk_00001")  # -> детерминированный UUID
```

До 2026-09-12 `point_id = uuid5(f"{document_id}-{i}")` — связь с графом была
возможна только через поиск. Смена схемы = полная переиндексация.

## 2. Структурные поля payload

Извлекаются из текста чанка (`src/indexing/standard_parser.py`) и кладутся в
payload верхнего уровня:

| Поле | Пример | Источник |
|---|---|---|
| `standard_number` | `ГОСТ Р 56545-2015`, `СТО БР БФБО-1.9-2024`, `ISO/IEC 27001:2022`, `Приказ ФСТЭК России № 17` | текст чанка |
| `clause` | `5.2.1` (из «п. 5.2.1», начало строки) | текст чанка |
| `section` | `7` (из «раздел 7») | текст чанка |

Извлекаются только явные упоминания — на произвольных числах ложных
срабатываний нет (проверено тестами).

## 3. Фильтры поиска

`POST /api/v1/chat/search`:

```json
{
  "query": "требования к защите информации",
  "limit": 5,
  "standard_number": "ГОСТ Р 56545-2015",
  "clause": "5.2.1"
}
```

- строка → точное совпадение (`MatchValue`);
- список → любое из значений (`MatchAny`), например
  `"standard_number": ["ГОСТ 57580.1-2017", "ГОСТ Р 56545-2015"]`;
- также доступны `section`, `document_id`, `file_type` (или всё то же внутри
  объекта `filters`).
- Права доступа (ACL) применяются по текущему пользователю.

## 4. Префикс метаданных в эмбеддинг

`build_embedding_text()` добавляет структурный префикс ТОЛЬКО к тексту документа:
`«ГОСТ Р 56545-2015, п. 5.2.1: <текст чанка>»`. Запрос эмбеддится как есть
(асимметрия). `content` в payload остаётся чистым — пользователь видит исходный
текст. BM25 (sparse) тоже видит префикс, поэтому поиск по номеру стандарта
работает и лексически.

## 5. Что делать существующим документам

Поля и новые `point_id` появляются только у вновь проиндексированных чанков.
Переиндексация: `POST /api/v1/upload/{document_id}/reindex` (внутри
`process_document(force=True)` — старые чанки удаляются, дублей не остаётся).

⚠️ Не забывайте про `force`: без него reindex оставлял старые векторы
(исправлено 2026-09-12, коммит f57433e).

## 6. Проверка после переиндексации

```bash
# payload и point_id в Qdrant
curl -s -X POST http://localhost:6333/collections/kag_documents/points/scroll \
  -H "api-key: $QDRANT_API_KEY" -H 'Content-Type: application/json' \
  -d '{"limit": 5, "with_payload": ["chunk_id","standard_number","clause"], "with_vector": false}'

# qdrant_point_id в Neo4j
docker exec kag-neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
  "MATCH (c:Chunk) WHERE c.qdrant_point_id IS NOT NULL RETURN c.id, c.qdrant_point_id LIMIT 5"

# оценка качества до/после
python scripts/evaluate_retrieval.py --output reports/eval_after.json
```
