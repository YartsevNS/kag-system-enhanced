# Внешняя оценка онтологии: сверка с живой схемой и первые правки

Источник: разбор в чате DeepSeek (шара i5b22d7rlmhoo1ukb9), сделанный по файлу ontology(1).txt.
Оценка там жёсткая: 3–5 из 10, «каркас, который надо переделывать до наполнения».

Задача этого документа — не спорить и не соглашаться на слово, а проверить каждое замечание
по фактической схеме в `cybersecurity_ontology_v2` (состояние на 08.11.2026, 03:5x UTC).

## Сверка замечаний с базой

| Замечание из оценки | Проверка по базе | Вывод |
|---|---|---|
| «ни один атрибут не объявлен `@key`» | `ent-key @key` стоит у всех 11 сущностей, у `certificate` добавлен | **устарело**: оценка сделана по старой версии файла |
| «`relates` без `@card`, всё опционально» | у всех 22 ролей стоит `@card(1..1)` | **устарело** частично: роли обязательны, но `owns` без `@card` — верно |
| «синтаксис `entity X; entity X owns Y;` неканонический» | этот текст живьём применяется в TypeDB 3.14.1 (validate + apply, схема 104 типа) | **неверно**: синтаксис рабочий |
| «нет связи версия документа → структурный элемент» | связи `document_structure` не было; 885 `structural_element` висят на `source-chunk-id` | **подтвердилось** |
| «`measure_definition:source_requirement` играет `structural_element`, а не требование» | именно так: `structural_element plays measure_definition:source_requirement`, связей 142 | **подтвердилось, это баг модели** |
| «`applicability_criteria` не связывает требование с объектом КИИ» | `applied_requirement` играют `security_measure` и `structural_element`, связей 316 | **подтвердилось** |
| «нет связи документа компании с нормативным» | связи нет | **подтвердилось** |
| «`threat_source` — сирота» | связей с требованиями/мерами/объектами нет | **подтвердилось** |
| «`is-certified` — boolean без сертификата» | так и есть, у `security_software` только `is-certified` | **подтвердилось** |
| «`current_version_string` дублирует `version_string`» | оба атрибута есть, связи «документ → текущая версия» нет | **подтвердилось** |
| «нет супертипов, атрибуты provenance копируются» | 11 сущностей дублируют 5 атрибутов provenance | **подтвердилось** |
| «`document_key`/`requirement-id`/`code` без уникальности» | `@key` только на `ent-key`; в данных 30 значений `document_key`, есть повторы (5× одинаковый, 2× «ГОСТ Р 57580.2-2018», 2× тестовый ГОСТ); у 23 из 47 документов ключа нет | **подтвердилось, и хуже: дубли уже в данных** |

Итог сверки: из 12 замечаний 2 относятся к старой версии файла, 10 подтверждаются живой базой.
Оценку «3 из 10» по семантике я считаю справедливой для состояния до этих правок.

## Что применено сразу (аддитивно, без удаления старого)

`POST /api/v1/ontology/apply` (validate → apply, version_id c38b283a6424…):

    document_structure        document_version → structural_element
    requirement_derived_from  security_requirement → structural_element
    requirement_definition    security_requirement → security_measure
    requirement_applicability security_requirement → cii_object
    threat_applicability      threat_source → cii_object
    document_compliance       company_document → regulatory_document
    current_version_of        документ → document_version
    certification             security_software → certificate (+ basis_requirement)
    entity certificate        ent-key @key, cert_number, cert_authority, cert_until, source_origin

Схема до/после:

    entities   11 → 12   (+ certificate)
    relations  11 → 19   (+8)
    attributes 31 → 34   (+3)
    типов      75 → 104
    отпечаток  6ba4afbfa64d208e → 2affe7cc10c84b0e

Проверено после применения: `violated_certification()` по-прежнему даёт факт
(«АСУ ТП цеха № 1» / «п. 11.1») — новые роли старые правила не сломали.

## Что НЕ сделано и почему (честно)

1. **`@key` на `document_key` поставить нельзя** — 23 из 47 документов без ключа, и в 30
   существующих есть повторы. Сначала нормализация ключей, потом ограничение.
2. **Старые связи `measure_definition` (142) и `applicability_criteria` (316) не переписаны**:
   удаление роли и пересоздание связей — миграция данных, её нельзя делать «на бегу».
   Новая связь `requirement_definition` введена рядом, данные в неё ещё не перелиты.
3. **Заполнение новых связей** (версия → пункт, требование → мера) требует правки writer'а
   и скрипта миграции по `source-chunk-id` — идёт следующим шагом.
4. **`current_version_string`** оставлен как есть до переливки в `current_version_of`.
5. **Промпт извлечения** не менялся: он должен опираться на новые связи, но менять его
   до заполнения данных — значит просить LLM писать в пустые места.
