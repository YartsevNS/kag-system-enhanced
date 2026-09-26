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

**Автоматическая проверка.** С 2026-09-12 после векторзации документ не просто
считается готовым: сервис делает exact count точек документа в Qdrant и сверяет
с числом чанков (`src/indexing/indexing_guards.py`, шаг `verify_points` в
process-логе документа). 0 точек при ненулевых чанках — провал индексации:
документ переводится в `failed` с понятной ошибкой, а не в `completed`.
Расхождение числа точек и чанков пишется warning'ом. Так закрыт класс ошибок
2026-09-12: документ «обработан, 180 чанков», а в Qdrant 0 точек (коллизии
point_id), и это было невидимо.

Проверить вручную (когда нужно убедиться самому):

```bash
# payload и point_id в Qdrant
curl -s -X POST http://localhost:6333/collections/kag_documents/points/scroll \
  -H "api-key: $QDRANT_API_KEY" -H 'Content-Type: application/json' \
  -d '{"limit": 5, "with_payload": ["chunk_id","standard_number","clause"], "with_vector": false}'

# qdrant_point_id в Neo4j
docker exec kag-neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
  "MATCH (c:Chunk) WHERE c.qdrant_point_id IS NOT NULL RETURN c.id, c.qdrant_point_id LIMIT 5"

# точное число точек документа
curl -s -X POST http://localhost:6333/collections/kag_documents/points/count \
  -H "api-key: $QDRANT_API_KEY" -H 'Content-Type: application/json' \
  -d '{"exact": true, "filter": {"must": [{"key":"document_id","match":{"value":"<id>"}}]}}'

# оценка качества до/после
python scripts/evaluate_retrieval.py --output reports/eval_after.json
```

## 7. Прочие предохранители (2026-09-12)

| Что | Где | Поведение |
|---|---|---|
| Проверка записи векторов | `document_service._process_document_impl` + `indexing_guards.check_points_written` | 0 точек → `failed`, расхождение → warning |
| Liveness вместо слепых 60 минут | `recovery.py` + `indexing_guards.recovery_reason` | задачи нет в active/reserved >5 мин → документ перезапускается сразу; порог 60 мин остаётся страховкой, когда `inspect` недоступен |
| Ретрай дедлоков Neo4j | `indexing_guards.run_with_transient_retry` в `batch_create_entities/relations` | при `TransientError.DeadlockDetected` — до 3 попыток; нетранзиентные ошибки пробрасываются (раньше пачка связей терялась молча) |
| Ошибка эмбеддинга ≠ «ничего не найдено» | `POST /api/v1/chat/search` | при отказе модели запроса — 503 вместо 200 с пустым списком |

Сверка рассинхрона Qdrant/Neo4j и БД: `scripts/cleanup_orphans.py`
(dry-run по умолчанию, `--apply` удаляет). Перезаливка текста чанков в граф:
`scripts/backfill_graph_chunk_text.py`.

## 8. Схема payload: как есть и что предлагается (2026-09-26)

### Как есть — реальные примеры из коллекции (9 929 точек, коллекция `kag_documents`)

Карточка документа (участвует в поиске как источник верхнего уровня):

```json
{
  "id": "0005eb5c-d9d1-5027-bef9-f7f7cb0512db",
  "payload": {
    "document_id": "765589a3-98d7-490c-9673-c2b25e4f3e64",
    "chunk_id": "card",
    "level": "document",
    "content": "Р 50.1.113—2016. Рекомендации по стандартизации. …",
    "filename": "r-50.1.pdf",
    "title": "Р 50.1.113—2016. Рекомендации по стандартизации. Информационная технология…",
    "document_type": "standard",
    "summary": "Документ представляет собой рекомендации по стандартизации…",
    "topics": ["криптографическая защита информации", "электронная цифровая подпись", "хэширование"],
    "visibility": "public",
    "allow_group_ids": [], "deny_group_ids": [],
    "allow_user_ids": [], "deny_user_ids": [],
    "domain": "infosec"
  }
}
```

Обычный фрагмент (страницы лежат ВНУТРИ вложенного `metadata`):

```json
{
  "id": "000ba86b-9274-5327-a94f-a1ff792d94e5",
  "payload": {
    "document_id": "ddb26a90-5157-40a3-ac91-76d2854615ca",
    "chunk_id": "ddb26a90-…_chunk_00107",
    "content": ") = TUV ∈ V768: …",
    "file_type": ".pdf",
    "filename": "r-1323565.1.pdf",
    "document_type": "standard",
    "domain": "infosec",
    "visibility": "public",
    "allow_group_ids": [], "deny_group_ids": [],
    "allow_user_ids": [], "deny_user_ids": [],
    "group_ids": [],
    "metadata": {
      "page_count": 40, "parser": "pymupdf", "tables_count": 17,
      "chunk_index": 106, "chunk_seq": 107, "total_chunks": 116,
      "splitter": "segment_based", "is_partial": false, "overlap_applied": true,
      "pages": [34, 35]
    }
  }
}
```

### Что из этого уже закрывает требования

| Требование | Состояние |
| :-- | :-- |
| ACL | ✅ плоские поля: `visibility` + `allow/deny_group_ids` + `allow/deny_user_ids`, плюс `group_ids` для pre-filter |
| Страница (откуда факт) | ⚠️ есть, но внутри `metadata.pages` (массив: фрагмент может пересекать страницы) и НЕ проиндексировано |
| Версионирование документа | ❌ в payload нет; версии лежат в Postgres (`document_versions`) |
| Связь с Neo4j | ⚠️ односторонняя: узлы графа несут `qdrant_point_id`, в payload узла нет — искать приходится по `chunk_id` |
| Табличный слой | ⚠️ `table_id`/`row_count` живут в Postgres, в payload их нет |

Индексировано сейчас (5 полей): `chunk_id`, `document_id`, `file_type`, `filename`, `group_ids`.
`domain` и `visibility` используются в фильтрах чата, но индекса не имеют — фильтр работает без ускорения.

### Предлагаемая схема (плоская, обратно совместимая)

Правило: всё, по чему фильтруем, сортируем или переходим — ПЛОСКИМ полем; вложенный `metadata`
оставляем как есть (в нём тайминги парсинга, `splitter`, `pages` — ломать совместимость незачем).

```json
{
  "id": "uuid5(chunk_id)",
  "payload": {
    "document_id": "ddb26a90-5157-40a3-ac91-76d2854615ca",
    "chunk_id": "ddb26a90-…_chunk_00107",
    "level": "chunk",
    "content": "…) = TUV ∈ V768: …",
    "filename": "r-1323565.1.pdf",
    "file_type": ".pdf",
    "document_type": "standard",
    "domain": "infosec",

    "visibility": "public",
    "allow_group_ids": ["g-analytics"],
    "deny_group_ids": [],
    "allow_user_ids": [],
    "deny_user_ids": [],
    "group_ids": ["g-analytics"],

    "page_start": 34,
    "page_end": 35,
    "pages": [34, 35],

    "document_version": 3,
    "version_valid_from": "2026-09-20T18:00:00Z",
    "version_valid_to": null,
    "is_current": true,

    "graph_node_id": "chunk:ddb26a90-…_chunk_00107",
    "table_id": null,
    "row_count": null,
    "standard_number": "ГОСТ Р 57580.1-2017",
    "clause": "5.2.1",

    "metadata": { "…как и раньше: parser, splitter, pages, tables_count, …": "…" }
  }
}
```

Поля и их назначение:

| Поле | Тип | Зачем | Индекс |
| :-- | :-- | :-- | :-- |
| `visibility`, `allow_/deny_group_ids`, `allow_/deny_user_ids`, `group_ids` | keyword / keyword[] | ACL pre-filter (уже работает) | `group_ids` есть, остальные не нужны (фильтр идёт по ним вместе) |
| `domain` | keyword | мягкий фильтр домена в чате | **нужен** |
| `visibility` | keyword | отсечение закрытых документов | **нужен** |
| `page_start`, `page_end` | integer | переход к странице, фильтр «только эта страница», подсветка | **нужен** |
| `document_version`, `is_current` | integer / bool | откат к версии, «искать только в актуальной» | `is_current` — **нужен** |
| `version_valid_from/to` | datetime | аудит и временные вопросы («что действовало тогда») | по потребности |
| `graph_node_id` | keyword | переход вектор → узел графа без поиска по `chunk_id` | не нужен (точечный get) |
| `table_id`, `row_count` | keyword / integer | связка с табличным слоем (SQL-ответ) | `table_id` — по потребности |

### Переход без простоя (blue-green, как в присланном разборе)

1. **Индексы добавляются сразу** на существующую коллекцию — они построятся по имеющимся точкам,
   а значения новых полей появятся по мере переиндексации. Простоя нет.
2. **Новые поля пишутся при обработке** документа и при `reindex_document` — то есть корпус
   наполняется постепенно, без остановки системы.
3. **Полная переиндексация (смена модели эмбеддингов, смена размерности) — только через псевдоним:**
   создать `kag_documents_v2`, залить, сверить (число точек, ACL-поля, наличие `page_start`/`domain`),
   переключить псевдоним `kag_documents_current` (в коде обращаться только к нему), старую коллекцию
   удалить через сутки. Сейчас псевдонимов нет, а в коде есть путь, который сносит коллекцию целиком
   (`delete_collection` при смене размерности) — это единственная операция, где система может остаться
   без поиска.

Проверка после переиндексации — по разделу 6 («Проверка после переиндексации») плюс сверка
`is_current`, `page_start` и ACL-полей на выборке.

## 9. Ответы на разбор схемы (2026-09-26)

Ответы на конкретные вопросы из внешнего разбора — с проверкой по коду и замерами, а не по памяти.

### 1. Правило разрешения конфликтов ACL (пользователь и в allow, и в deny)

**Побеждает deny.** Реализовано в двух местах:

- pre-filter в Qdrant (`src/indexing/embeddings_service.py`, ~строка 769): «доступно, если
  public ИЛИ пользователь/группа в allow» собирается как `should`-группа, а «запрещено, если
  пользователь/группа в deny» — как `must_not`. `must_not` применяется независимо от `should`,
  поэтому deny сильнее allow и даже владельца;
- post-guard `_access_guard` (та же логика через `can_read`) — на случай, если документ
  попал в выдачу обходным путём.

Тесты: `tests/test_document_access.py` — `test_deny_has_priority`, `test_deny_beats_owner`,
`test_restricted_hidden_from_others`, `test_legacy_group_ids_restrict_access`.

Отдельно про «пустой allow + пустой deny = доступ всем»: в нашей схеме это не так.
`visibility="restricted"` без записей в allow-списках не проходит `should`-группу
(там только `visibility=public`, `allow_group_ids`, `allow_user_ids`), поэтому такой документ
виден только администратору и владельцу — то есть «fail closed», а не «доступ всем».

### 2. Замер фильтра по `domain` до и после индекса

Методика: 300 запросов напрямую в Qdrant (имя вектора `dense`), коллекция 9 929 точек,
фильтр `domain=infosec` подходит к 4 401 точке. Индекс создаётся мгновенно (0,3 с).

| Состояние | Без фильтра (p50) | С фильтром (p50) | Стоимость фильтра |
| :-- | :-- | :-- | :-- |
| до индекса | 11,10 мс | 35,28 мс | **+24,18 мс на каждый запрос** |
| после индекса | 11,12 мс | 10,83 мс | ~0 (в пределах шума) |

Ускорение фильтрованного запроса — **3,3×** (35,28 → 10,83 мс). Индекс на `domain` **создан на
стенде** этой проверкой (0,3 с, обратимо через `delete_payload_index`). Честная оговорка: в общем
времени ответа чата (~3,5 с) это доли процента — выигрыш не в сегодняшней скорости, а в том, что
штраф за фильтр растёт с размером коллекции; на миллионах точек он стал бы заметным.
Такие же индексы нужны `visibility` (используется в том же фильтре) и `is_current`/`valid_to`.

### 3. Обновление `valid_to` при выходе новой версии документа

Важно: **сегодня версионирования нет** — таблица `document_versions` в Postgres пустая (0 строк),
переиндексация документа заменяет фрагменты на месте (delete + upsert). Ниже — псевдокод того,
как это делать, если версии понадобятся (с учётом замечания «не вводить `is_current`, а
использовать `valid_to is null`» — согласен, это атомарнее):

```python
def publish_new_version(doc_id: str, text: str) -> None:
    now = utcnow()
    new_hash = sha256(text)                       # content_hash: ловит «то же имя, другой текст»
    with transaction():                           # Postgres — источник правды
        old = versions.current(doc_id)            # where valid_to is null
        if old and old.content_hash == new_hash:
            return                                # текст не менялся — переиндексация не нужна
        versions.insert(doc_id, new_hash, valid_from=now, valid_to=None)
        if old:
            versions.close(old, valid_to=now)     # ОДНО обновление; «текущая» = valid_to is null
    qdrant.set_payload(                          # закрываем старые точки пачкой
        filter={"document_id": doc_id, "valid_to": None},
        payload={"valid_to": now.isoformat()})
    chunks = parse_and_chunk(text)
    points = embed(chunks, payload={"document_id": doc_id, "valid_from": now.isoformat(),
                                    "valid_to": None, "content_hash": new_hash,
                                    "schema_version": SCHEMA_VERSION})
    qdrant.upsert(points)                        # новые точки считаются текущими
```

Поиск «только актуальное» — фильтр `valid_to is null`, без отдельного `is_current`. Откат версии —
закрыть текущую (`valid_to=now`) и вернуть прежние точки (`valid_to=null`), не пересчитывая векторы.

### 4. Есть ли в Neo4j узлы, которые не являются фрагментами

Да, четыре вида узлов (замер на стенде 26.09.2026):

| Метка | Узлов | Свойства |
| :-- | :-- | :-- |
| `Entity` | 34 261 | `name`, `type`, `description`, `confidence`, `source_docs`, `properties`, `updated_at` |
| `Chunk` | 9 814 | `id`, `qdrant_point_id`, `text`, `section_id`, `breadcrumb`, `chunk_seq`, `updated_at` |
| `Section` | 301 | раздел документа (узел-раздел, к которому привязаны фрагменты) |
| `Document` | 230 | `id`, `filename`, `metadata`, `updated_at` |

Отсюда ответ на вопрос про `graph_node_id`: отдельное поле не нужно — `chunk_id` и есть
`Chunk.id`, то есть идентификатор узла графа, общий для обеих систем. Связь односторонняя
(граф → вектор, через `qdrant_point_id`) и этого достаточно; `graph_node_id` в payload создал бы
циклическую зависимость без выигрыша.

### Что принято из разбора, а что отклонено (с причинами)

**Правило эволюции уровней доступа** (замечание разбора: «если появится visibility=secret — фильтр
должен это учитывать»). Как ведёт себя фильтр сейчас и что помнить при добавлении третьего уровня:

| Значение `visibility` | Кто видит (не админ) |
| :-- | :-- |
| `public` | все |
| `restricted` + пустые allow-списки | никто, кроме владельца и админа (система закрывается, а не открывается) |
| `restricted` + allow-группы/пользователи | перечисленные (+ владелец, админ) |
| поле ОТСУТСТВУЕТ | считается `public` — сделано для совместимости со старыми документами |
| любое НЕИЗВЕСТНОЕ значение (`secret`, опечатка) | не `public` → доступ только по allow-спискам, то есть fail-closed |

Отсюда правило на будущее: новый уровень доступа достаточно завести значением, отличным от
`public`, и он по умолчанию будет закрытым. Опасна только обратная ситуация — если появится
значение, которое должно быть ШИРЕ `public` (например, «виден и анонимам»), тогда фильтр нужно
править осознанно: сейчас аноним видит ровно `visibility=public`.

| Совет | Решение | Почему |
| :-- | :-- | :-- |
| `acl_mode: public/restricted` | **отклонено** | роль маркера уже играет `visibility`; «fail closed» на пустых allow-списках проверен кодом и тестами |
| Удалить `group_ids` как устаревшее | **отклонено** | поле используется (legacy-группы, оно же проиндексировано); 9,9 тыс. точек — это ~150 КБ, экономить нечего |
| Выбрать одно: `pages[]` или `page_start/page_end` | **принято** | оставляем `pages[]` + nested-индекс; `min/max` для интерфейса считаем на месте |
| Вместо `is_current` — `valid_to is null` | **принято** | одно атомарное обновление вместо двух точек |
| `content_hash` | **принято** | дедупликация «тот же текст под другим именем» и защита от лишней переиндексации |
| `graph_node_id` в payload | **принято частично** | не нужно как поле: `chunk_id` = `Chunk.id`; односторонней связи достаточно |
| `standard_number`/`clause` в `attributes` | **отложено** | сейчас это плоские поля; вложенность потребует путей в индексах без выигрыша. Нужны только индексы (как у `domain`) |
| `schema_version` | **принято** | дешёвая страховка при эволюции схемы |
| `ingested_at` отдельно от `valid_from` | **принято** | «когда залили» и «когда действует» — разные вещи, для отладки нужно первое |
| `language` | **принято как низкий приоритет** | в корпусе есть английские документы, но эмбеддинги многоязычные |
| `trace_id` в payload | **согласен — не в payload** | генерируется на входе API, идёт заголовком и в логи; в Qdrant возвращаем `chunk_id` |
