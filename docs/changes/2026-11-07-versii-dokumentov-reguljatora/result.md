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

## Итерация 2 (P0.3–P0.5) — 07.11.2026

**Сделано:**

    upload.py            поле number (Form) + source_metadata["number"]
    typedb_versions.py   document_key = издатель|тип|номер (номер из metadata, а не из названия)
                         effective_to прежней редакции = effective_from новой
                         (вставка без delete: у версии effective_to 0..1, атрибута ещё нет)
    documents.html       поле «Номер документа»

**Проверено на стенде:**

    diag двумя редакциями: {'superseded': 1, 'effective_to': 1}
    действующих редакций: 1   (по отсутствию входящего supersedes)

**Backfill document_key (`/tmp/backfill_dk.py`):**

    regulatory_document: всего 48, проставлено 22 уникальных названия, с ключом 26
    company_document:    всего 2, проставлено 2

Причина неполноты: у старых актов нет `number`/`doc_type`/издателя — ключ строится только
от названия, а 22 названия пустые или совпадают с уже обработанными. Ключ уточнится при
переобработке документа (там есть issuer/doc_kind/number из формы).

**Попутно:** у `company_document` не было `owns document_key` — добавлено (define).

## Итерация 3 (P1.6) — 07.11.2026

**Проблема:** `issuer` и `doc_kind` лежали в `source_metadata` и payload Qdrant, но в онтологию
не попадали: `regulator` не создавался, `doc_type` у акта оставался пустым, ключ акта не ставился
существующему объекту.

**Сделано** (`src/indexing/typedb_versions.py`, блок 3b):

    regulator              ищется по name, при отсутствии создаётся (ent-key от названия)
    document_issuance      связь регулятор → акт, если ещё нет
    doc_type               проставляется акту из doc_kind («приказ», «ГОСТ»)
    document_key           проставляется существующему акту, если отсутствует

**Проверено на стенде (`/tmp/verify_issuer.py`, акт «Приказ ФСТЭК России 999»):**

    upsert: {'created': 1, 'linked': 1, 'issuer_linked': 1}
    regulator «ФСТЭК России»: 1
    document_issuance к акту: 1
    doc_type «приказ»: 1
    document_key акта: 1

**Замечание по чистке:** удаление акта без снятия `document_issuance` падает
(`CNT5` — роль `regulated_item` опустела). Удаление документа в продукте пока не чистит
онтологию вовсе (техдолг 17); при реализации каскада сначала снимать связи.

**Легенда приоритетов** (рабочая шкала, не стандарт): P0 — ломает уже сделанное;
P1 — функциональный пробел; P2 — операционный долг (выкат, чистка, тесты).

## P0 НЕ ЗАКРЫТ: сквозной прогон 07.11.2026 (реальные документы)

Проверял не модуль напрямую, а флоу: загрузка через сайт → обработка → онтология → Qdrant.

    ID1 = d1d4e035-586f-493f-bc7f-ae686257d41f  (ГОСТ Р 99999-2015, version_string=2015)
    ID2 = d3099c6e-cab5-4641-81ca-d5f8cbe6d500  (ГОСТ Р 99999-2021, supersedes_id=ID1)

Оба завершились `completed`. Факты из TypeDB:

    версия 2015: 1
    версия 2021: 1
    связь версия-акт 2015: 0
    supersedes 2021->2015: 0
    effective_to у 2015: 0
    акт с document_key «росстандарт|гост|99999»: 0
    doc_type у акта: 0
    regulator «Росстандарт»: 0
    исходящая связь акта (document_issuance): 0

То есть объекты версий создаются, но **пустые и несвязанные**. В БД при этом всё на месте:
`recognized_title` = «ГОСТ Р 99999-2015. Тестовый стандарт защиты информации»,
`source_metadata` = JSON с issuer/doc_kind/number/version_string/effective_from/supersedes_id.

Причина (локализована):

1. В `_build_typedb_only` название для поиска акта берётся как
   `(source_metadata or {}).get("title") or filename` → это `gost2015.md`, а writer создал акт
   с названием из текста документа. Акт не найден → `linked=0`, и весь блок 3b не выполняется.
2. `supersedes` не создан, значит `supersedes_id` не дошёл до `upsert_from_metadata`
   (ожидаемо, если metadata пустая на входе воркера) — требует проверки тем же прогоном
   после правки.

Что править (следующий шаг, без пересборки образа — dev-монтирование):

    document_service._process_document_impl   передавать title = record.recognized_title
    document_service._build_typedb_only       если source_metadata пустой — брать из БД по document_id
                                              (не зависеть от кэша записи в воркере)
    typedb_versions.upsert_from_metadata      при совпадении по номеру нормализовать и название
                                              (акт writer'а: title «ГОСТ Р 99999-2015. …»)
    затем                                     удалить две пустые версии, переобработать оба документа,
                                              повторить те же 9 проверок до 1/1/1/1/1/1/1/1/1

## Прогон 2 (после правки title/metadata/акт по чанк-префиксу) — 07.11.2026

Правки: `_build_typedb_only` читает `source_metadata` и `recognized_title` из БД, если в записи
воркера их нет; акт ищется по префиксу `source-chunk-id` документа (`"<document_id>.*"`),
потом по номеру+типу, потом по ключу, потом по названию.

Лог воркера `kag-system_worker_1` (19:42):

    [versions] документ d1d4e035…: {'created': 1, 'linked': 1, 'superseded': 0, 'issuer_linked': 1}
    [versions] документ d3099c6e…: {'created': 1, 'linked': 1, 'superseded': 1, 'issuer_linked': 1, 'effective_to': 1}
    [versions] прежняя редакция d1d4e035…: помечено чанков 1982

Факт в базе `cybersecurity_ontology_v2` сразу после (скрипт `/tmp/cv.py`):

    версий всего: 1
    единственная версия: ent-key=issuever-1, kag_document_id=issuever-1 (тестовая от P1.6)
    d1d4e035: 0 версий
    d3099c6e: 0 версий

То есть запись отрапортовала успех, но объектов в базе нет. Расхождение не объяснено;
возможные причины (проверять по порядку): (1) `_cfg()` в модуле берёт базу из настроек
приложения, а не из env контейнера, и запись ушла в другую базу; (2) транзакция коммитится,
но затем документ пересоздаётся/чистится другим проходом; (3) в контейнере осталась старая
копия модуля (`/app/src` против монтирования). Первый шаг следующей итерации — напечатать
из модуля адрес и имя базы в момент записи (лог в `upsert_from_metadata`) и повторить прогон.

**Вывод:** сквозной флоу версий по-прежнему не подтверждён. P0 остаётся открытым.

## ПРИЧИНА НАЙДЕНА: глотаемое исключение внутри транзакции откатывает всю пачку — 07.11.2026

Прямые замеры:

    probe из контейнера воркера (документа нет в базе):  created 1, after_commit 1  — сохраняется
    прямой вызов для d1d4e035 (документ есть в базе):    created 1, after_commit 0  — не сохраняется
    повторный вызов для того же документа:               снова created 1 (объекта нет)

Версия пишется в той же транзакции, что и блок «издатель/тип/ключ» (3b). Внутри 3b стоит
`try/except`, который глотает ошибку insert (например, повторная вставка `doc_type`, связи
`document_issuance` при двух актах на документ, вставка ключа). TypeDB после первой неудачной
операции помечает транзакцию ошибочной, все последующие insert'ы молча не применяются, а
`commit` откатывает пачку целиком. Наружу при этом уходит «created 1», потому что счётчик
пишется в Python-переменную до коммита.

**Следствие:** в TypeDB остаётся 0 версий, хотя лог воркера рапортует успех. Ровно этот же
механизм объясняет, почему в первом прогоне версии «создавались и исчезали».

**Что делать (следующая итерация):** разбить `upsert_from_metadata` на независимые транзакции:
(1) версия + `document_version_of` + `supersedes` + `effective_to` — своя транзакция и коммит;
(2) издатель/тип/ключ акта — отдельная транзакция, её сбой не влияет на версию;
(3) контроль — отдельным соединением; результат в лог.
Ошибку в (2) логировать как warning, а не глотать молча.
