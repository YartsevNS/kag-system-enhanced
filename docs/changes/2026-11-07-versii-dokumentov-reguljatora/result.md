# Результат: версии документов регулятора

Дата: 2026-11-07. Статус: ядро выполнено и проверено на стенде; поиск проверен кодом и тестом,
прогон на живых двух документах не выполнялся.

## Что сделано

| Что | Файл / объект | Состояние |
|---|---|---|
| схема | `cybersecurity_ontology_v2` | entity `document_version`, relations `document_version_of`, `supersedes`, атрибуты `effective_from`, `effective_to`, `version_status`, `version_hash`, `kag_document_id`, `document_key`, `current_version_string`; `plays` у актов и версии |
| файлы схемы | `project_typedb/adr/schema-v2.tql`, `functions-v2.tql` | блок версий и `current_versions()` добавлены (аддитивно) |
| модуль версий | `src/indexing/typedb_versions.py` | `normalize_document_key`, `_date_literal`, `upsert_from_metadata` (идемпотентно) |
| конвейер | `src/api/services/document_service.py` | вызов после записи онтологии, `source_metadata` пробрасывается |
| загрузка | `src/api/routes/upload.py` | `version_string`, `effective_from`, `supersedes_id`; `VERSION_REQUIRED` для регулятора |
| UI | `src/api/static/documents.html` | поля редакции и даты (обязательны для регулятора), id заменяемой редакции |
| поиск | `src/indexing/embeddings_service.py` | payload: `version_string`, `version_status`, `effective_from`, `document_key`; `include_superseded` (по умолчанию False) |
| устаревшие ссылки | `src/indexing/typedb_facts.py` | `outdated_versions()`, `outdated_references(visible_chunk_ids)` с ACL по чанку |
| тесты | `tests/test_document_versions.py` | 7 passed |

## Проверка на стенде 192.168.50.18

`/tmp/diag_versions.py` — акт `regulatory_document` (ent-key `diag-gost-99999`) + две редакции:

    запись 2015: {'created': 1, 'linked': 1, 'superseded': 0}
    запись 2021: {'created': 1, 'linked': 1, 'superseded': 1}
    версий: 2
    связей с актом: 2
    supersedes: 1
    действующих (без входящего supersedes): 1
    повторная запись: {'created': 0, 'skipped': 'version exists'}
    после чистки тестовых объектов: версий 0

Загрузка через API:

    POST /api/v1/upload/ (source_kind=regulator, без version_string/effective_from)
    -> 400 {"detail": {"code": "VERSION_REQUIRED", ...}}

## Закрытые пункты прошлой итерации

1. «Связь с актом: 1 при двух версиях» — **была ошибка проверочного запроса**, не вставки.
   Корректный запрос через роль `document` даёт 2 связи на 2 версии.
2. `current_versions()` — функция объявлена и компилируется; вызов
   `match $v isa document_version; let $c = current_versions();` выполняется без ошибки.
   Итерация по результату функции (`$v in $c`) в TypeQL 3 этой версии не парсится —
   в продукте не используется, эквивалент проверен запросом «версия без входящего supersedes» (даёт 1).
3. Файлы `project_typedb/adr/` обновлены (schema-v2.tql, functions-v2.tql).

## Отклонения от design

1. **Статус прежней редакции не переписывается атрибутом.** TypeQL 3 отвергает
   `delete $o has $s` и `delete $o has version_status $s` (ошибка `expected OF`).
   Действующая редакция вычисляется моделью: версия без входящего `supersedes (older)`.
   `effective_to` у прежней автоматически не проставляется; `version_status` остаётся для
   «отменён без замены» (`repealed`).
2. **`document_key` по умолчанию строится от названия**, если номер не передан:
   отдельного поля «номер» в форме нет. Ключ: издатель + тип + (номер | название).
3. **`published_at` не введён** — в модели достаточно `effective_from`.

## Открыто

- [ ] Прогон поиска на двух живых документах (в Qdrant пока нет чанков с `version_status`
      `superseded`, поэтому фильтр нечем проверить). Проверено тестом
      `test_search_excludes_superseded_by_default` и кодом.
- [ ] Поле «номер документа» в форме загрузки: сейчас ключ акта строится от названия,
      из-за чего «ГОСТ Р 57580.1-2017» и та же редакция с другим пробелом в названии
      могут дать разные ключи.
- [ ] Автоопределение замены по web monitor (сверка `document_key` + `version_string`,
      предложение владельцу в админке) — не реализовано, вне рамок этого изменения.
- [ ] `document_key` у существующих актов не проставлен (в базе 47 `regulatory_document`
      без ключа) — заполнится при переобработке документов или отдельным скриптом.

## Итерация 1 (P0.1–P0.2) — 07.11.2026

**Проблема:** фильтр `version_status=superseded` нечего было отсекать: в payload статус всегда
`current` (загрузка его не писала), а прежняя редакция в Qdrant не переразмечалась.

**Сделано:**

    src/api/routes/upload.py             source_metadata получает version_status="current"
    src/api/services/document_service.py при supersedes_id и успешной замене — переразметка
                                         прежней редакции: update_document_payload(sup_id,
                                         {"version_status": "superseded"})
    src/api/routes/chat.py               /chat/search принимает include_superseded
    src/indexing/embeddings_service.py   update_document_payload возвращает ЧИСЛО точек,
                                         а не operation_id (в логах было «1955» при 49 чанках)

**Проверено на стенде 192.168.50.18:**

    health 200
    POST /chat/search без флага          -> total 2
    POST /chat/search include_superseded -> total 2   (superseded-чанков в базе пока нет)
    update_document_payload(913ba708, {version_status: current}) -> обновлено точек: 49
    payload чанков: ('913ba708…_chunk_00010', 'current')

49 = 48 фрагментов + карточка документа — совпадает с ожидаемым числом точек.
