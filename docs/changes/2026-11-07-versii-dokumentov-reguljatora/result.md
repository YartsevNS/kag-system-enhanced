# Результат: версии документов регулятора

Дата: 2026-11-07. Статус: частично выполнено — ядро работает, два пункта открыты.

## Что сделано

| Что | Файл | Состояние |
|---|---|---|
| схема онтологии | `cybersecurity_ontology_v2` | применено: entity `document_version`, relations `document_version_of`, `supersedes`, атрибуты `effective_from`, `effective_to`, `version_status`, `version_hash`, `kag_document_id`, `document_key`, `current_version_string`; `plays` у актов |
| модуль версий | `src/indexing/typedb_versions.py` | новый: ключ акта, дата, запись версии, связь с актом, `supersedes` |
| writer-путь | `src/api/services/document_service.py` | после записи онтологии вызывается `upsert_from_metadata` (идемпотентно) |
| загрузка | `src/api/routes/upload.py` | поля `version_string`, `effective_from`, `supersedes_id`; для регулятора `VERSION_REQUIRED`; всё в `source_metadata` |
| UI | `src/api/static/documents.html` | поля редакции и даты, обязательные для регулятора, плюс id заменяемой редакции |
| поиск | `src/indexing/embeddings_service.py` | payload: `version_string`/`version_status`/`effective_from`/`document_key`; `include_superseded` (по умолчанию False → `version_status=superseded` отсекается) |
| устаревшие ссылки | `src/indexing/typedb_facts.py` | `outdated_versions()`, `outdated_references(visible_chunk_ids)` с ACL по чанку |
| тесты | `tests/test_document_versions.py` | 7 passed |

## Проверка на стенде (192.168.50.18)

Скрипт `/tmp/verify_versions.py` создаёт акт `regulatory_document` (ent-key `test-gost-99999`,
title «ГОСТ Р 99999 Тестовый стандарт») и две редакции через `upsert_from_metadata`:

    2015: {'created': 1, 'linked': 1, 'superseded': 0}
    2021: {'created': 1, 'linked': 1, 'superseded': 1}
    повтор 2021: {'created': 0, 'skipped': 'version exists'}
    версий всего: 2; supersedes: 1; effective_from 2021-09-01: 1
    после чистки тестовых объектов: версий 0

То есть запись версии, привязка и цепочка замены работают; повторная обработка версий не плодит.
Тестовые объекты удалены, база в исходном состоянии по `document_version`.

## Отклонения от design (и почему)

1. **Статус старой редакции не переписывается атрибутом.** TypeQL 3 в этой версии не принимает
   ни `delete $o has $s`, ни `delete $o has version_status $s` (ошибка `expected OF`).
   Поэтому «действующая редакция» вычисляется по модели: версия без входящего `supersedes (older: …)`;
   `version_status` остаётся для случаев «отменён без замены» (`repealed`).
   Следствие: `effective_to` у прежней редакции не проставляется автоматически.
2. **Функция `current_versions()` не подтверждена.** Определение через
   `not { $s isa supersedes (older: $v); }` требует `plays supersedes:older` (добавлено),
   но финальная проверка функции не выполнена — открытый пункт.
3. **Проверочный запрос связи с актом вернул 1 строку при двух версиях** — либо ошибка запроса
   в скрипте (не группирует), либо вторая связь не создалась. Требует точечной проверки.

## Открыто

- [ ] Подтвердить `current_versions()` на двух редакциях (ожидание: 1).
- [ ] Разобрать «связь с актом: 1 при двух версиях».
- [ ] Проверить путь загрузки через API: регулятор без версии → 400 `VERSION_REQUIRED`.
- [ ] Проверить поиск: `superseded` не попадает в выдачу, `include_superseded=true` — попадает.
- [ ] Обновить `project_typedb/adr/schema-v2.tql` и `functions-v2.tql` новыми определениями
      (на стенде применено скриптами, в файлах схемы ещё нет).
