# Онтология v3 и промпт извлечения v3

Основание: внешняя оценка (DeepSeek) + решение владельца 08.11.2026: «база в TypeDB однозначно
кривая, не надо её жалеть, нужно будет поднимем новую; сейчас работаем по онтологии и по промпту».

## Что переделано в схеме (src/ontology/schema-v3.tql)

1. Абстрактные супертипы вместо копирования атрибутов:
   `identified_entity @abstract, owns ent-key @key` → `sourced_entity` (provenance) →
   `document` → `regulatory_document` / `company_document`. Наследование в TypeDB одиночное,
   поэтому цепочка, а не множественное.
2. Ключи: `ent-key @key` у всех, `document_key @key` у документов, `requirement-id @card(1..1)`,
   `cert_number @card(1..1)` у сертификата.
3. Обязательность: `title @card(1..1)`, `name @card(1..1)`, `version_string @card(1..1)`,
   `requirement_kind @card(1..1)`; все роли связей `@card(1..1)`.
4. Трассировка связями:
   `document_structure` (версия → пункт), `requirement_derived_from` (требование → пункт),
   `measure_source` (мера → пункт). Строки source-* остаются как денормализация.
5. Исправлен баг v2: «мера реализует требование» — это `requirement_definition`
   (security_requirement → security_measure), а не `measure_definition` с ролью source_requirement,
   которую играл structural_element.
6. Требование применимо к объекту напрямую: `requirement_applicability`; мера — `measure_applicability`.
7. Сертификация: `entity certificate` (cert_number, cert_authority, cert_until) + `certification`
   (certified_software, certificate, basis_requirement) вместо boolean.
8. Угрозы: `threat_applicability` (угроза → объект), `threat_measure` (угроза → мера).
9. Документы: `document_compliance` (документ компании → нормативный), `current_version_of`
   (документ → редакция) вместо строки current_version_string.

## Проверка на пустой базе cybersecurity_ontology_v3

Не «применилось», а поведение:

    схема применена (139 типов с наследниками), функции применены
    документ без title                    → отвергнут
    требование без requirement-id         → отвергнут
    дубль document_key                    → отвергнут (после добавления @key;
                                            первый прогон показал, что без @key дубль проходит)
    violated_certification на СЗИ без сертификата → «АСУ ТП цеха № 1 / п. 11.1»
    violated_documents → «Политика ИБ / п. 11.1»
    выдали сертификат СЗИ → violated_certification пуст
    current_versions при двух редакциях и supersedes → «2026» (не «2018»)

## Промпт v3

Сохранён в БД как версия 6 (история прежних версий сохранена), 7722 знака.
Что добавлено к авторскому тексту:
- обязательные атрибуты, без которых TypeDB отвергает запись;
- разделение «акт» и «редакция» (document_key + document_version + current_version_of);
- правило трассировки: каждый пункт привязан к версии, каждое требование — к пункту,
  мера — к требованию и к пункту;
- сертификат как объект, а не boolean;
- запрет устаревших measure_definition / applicability_criteria / current_version_string;
- плейсхолдер {text_chunk} (требование валидатора).

Заводской текст в коде (DEFAULT_ENTITY_PROMPT) обновлён тем же текстом, поэтому «Сбросить»
на странице возвращает v3, а не старую схему.

## Что осталось до переключения базы

`src/indexing/typedb_writer.py` знает только 7 типов и 5 связей, включая устаревшие
(measure_definition, applicability_criteria с ролью structural_part). Пока writer не обновлён,
переключать TYPEDB_DB на v3 нельзя: извлечение будет писать в несуществующие связи.
