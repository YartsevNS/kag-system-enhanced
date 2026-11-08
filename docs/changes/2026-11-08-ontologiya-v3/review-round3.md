# Третий круг: ролевая целостность функциями (08.11.2026)

Ревьюер подтвердил то, что показала база: в TypeDB 3.14 `@key`/`@unique` на ролях
(`relates`) не поддерживаются (issue typedb/typedb #7030 «Role players as keys»).
Аннотации остаются только для владения атрибутами (`owns`).

Итог разделения:

    схема (@key/@unique)        ent-key, document_key, requirement_key, measure_key,
                                cert_number — проверено, дубли отвергаются
    функции (*_violations)      одна текущая редакция на документ; один родитель у пункта;
                                пункт в одной редакции; несколько издателей
    проверка перед коммитом     src/indexing/typedb_integrity.py, вызывается после записи

## Что реализовано

Функции (src/ontology/functions-v3.tql, 10 функций):

    multiple_current_versions()        -> document, document_version
    multiple_parents()                 -> structural_element, structural_element, structural_element
    element_in_multiple_structures()   -> structural_element, document_version, document_version
    multiple_issuers()                 -> document, regulator, regulator
    elements_without_version()         -> structural_element
    versions_without_document()        -> document_version
    + violated_certification($today), violated_documents($today),
      current_versions(), expired_certificates($today)

Функции возвращают сами нарушения с участниками, а не «есть/нет»: видно, что чинить.

Проверка перед коммитом (src/indexing/typedb_integrity.py):

    check_integrity() → список нарушений [{check, problem, subject}]
    вызывается в document_service._build_typedb_only после записи документа
    находит функции по имени; если функции в базе нет (схема v2) — пропускает проверку,
    а не падает; результат пишется в лог и в журнал обработки (шаг integrity)

## Проверка

База cybersecurity_ontology_v3, тестовые данные:

    multiple_current_versions    → 2 нарушения (документ с двумя текущими редакциями)
    multiple_parents             → 0
    element_in_multiple_structures → 0
    multiple_issuers             → 0
    elements_without_version     → 0
    versions_without_document    → 0
    expired_certificates(2026-11-08) → 1 (СФ-OLD)

В контейнере kag-api, активная база v2:

    check_integrity() → 0 нарушений, проверки помечены «недоступна в этой схеме»,
    обработка не падает

## Промпт

Версия 8 (8 961 знак): добавлено «у пункта ОДИН родитель — не создавай вторую
document_hierarchy» и «только ОДНА current_version_of на документ».

## Ограничение, которое остаётся

Проверка идёт ПОСЛЕ записи: если нарушение найдено, документ уже записан.
Полный откат потребует обернуть запись в одну транзакцию — это следующий шаг вместе
с переписыванием writer под v3. Сейчас нарушение фиксируется в логе и журнале обработки,
а не откатывается.
