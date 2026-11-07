# Требования: версии документов регулятора

## R1. Сущность версии существует в схеме

**Дано:** база `cybersecurity_ontology_v2` от 07.11.2026
**Когда:** выполнен `define` новых типов
**Тогда:** `document_version` принимает объект; `regulatory_document` владеет `document_key`

**Как проверить:** `match $v isa document_version;` не падает; `match $x isa regulatory_document, has document_key $k;` не падает.

## R2. Writer создаёт ровно одну версию на документ

**Дано:** документ регулятора из 2 чанков, загружен с `version_string = "2015"`
**Когда:** выполнена обработка
**Тогда:** `document_version` по этому документу — 1 объект; `ent-key = <document_id>`

**Как проверить:** `match $v isa document_version, has kag_document_id "<id>";` -> 1 результат; повторная обработка -> по-прежнему 1.

## R3. Версия связана с актом, ключ акта общий для редакций

**Дано:** две редакции одного ГОСТ (`2015`, `2021`)
**Когда:** обе загружены и обработаны
**Тогда:** один `regulatory_document` c одним `document_key`; две `document_version`; на акте `current_version_string = "2021"`

**Как проверить:** `match $d isa regulatory_document, has document_key $k; $v (document: $d) isa document_version;` -> 2 версии.

## R4. Замена редакции фиксируется связью и статусами

**Дано:** редакция `2015` уже в базе
**Когда:** загрузка `2021` с `supersedes_id = "<id 2015>"`
**Тогда:** `document_version 2015` имеет `version_status "superseded"` и `effective_to` = `effective_from` новой; связь `supersedes` (2021 -> 2015)

**Как проверить:** `match $n isa document_version, has version_status "superseded"; $n (newer: $o) isa supersedes;`.

## R5. Обязательность полей при загрузке регулятора

**Дано:** POST `/api/v1/upload/` с `source_kind=regulator`
**Когда:** не передан `version_string` или `effective_from`
**Тогда:** 400 с кодом `VERSION_REQUIRED`; документ не создан

**Как проверить:** curl -> `{"code": "VERSION_REQUIRED"}`; `select count(*) from documents` не вырос.

## R6. Поиск по умолчанию не отдаёт утратившие силу редакции

**Дано:** в Qdrant чанки двух редакций, у старой `version_status = superseded`
**Когда:** POST `/api/v1/chat/search` без параметров
**Тогда:** чанки старой редакции в выдаче отсутствуют; с `include_superseded=true` — присутствуют

**Как проверить:** сравнение `document_id` в топ-10 при двух вызовах.

## R7. Правило показывает устаревшие ссылки

**Дано:** требование, чей чанк принадлежит `superseded`-версии
**Когда:** вызов `outdated_references()`
**Тогда:** требование и дата окончания действия в результате

**Как проверить:** вызов функции через драйвер; результат непустой при наличии таких требований.

## R8. ACL не ослабевает

**Дано:** версия документа, чьи чанки не видны пользователю
**Когда:** `query_facts(visible_chunk_ids)` для этого пользователя
**Тогда:** фактов по версии — 0, даже если версия `current`

**Как проверить:** повтор теста ACL из `docs/eval/typedb-contour-check-2026-11-07.md`.

## Ограничения

- не трогать базу `cybersecurity_ontology` (точка отката);
- массовый прогон корпуса не запускать: версии появятся у документов при следующей обработке;
- `document_key` не должен зависеть от порядка слов в названии — только нормализованные тип/номер.
