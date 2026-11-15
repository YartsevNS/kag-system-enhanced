# Схема онтологии KAG — документ для аудита

Сформирован: 15.11.2026 16:29
Источник: `src/ontology/core.tql` (ядро) и `src/ontology/modules/*.tql` (доменные модули).
Это ровно те файлы, из которых собирается и загружается схема в TypeDB.

## 1. Что это за онтология

Онтология нормативных требований и мер защиты: документы регуляторов (ФСТЭК, ФСБ, Банк России, ГОСТ),
их структура (разделы, пункты, приложения), требования, меры (контроли), доказательства, редакции
документов, а также родовые понятия — субъекты, активы, данные, угрозы, уязвимости, риски,
инциденты, нарушения, термины, функции и процессы.

Контур продукта: документ компании → его пункты → покрытие требований регуляторов → пробелы →
новая редакция документа со ссылками на требования.

Архитектурное решение (ADR-010): **ядро доменно-независимо, домены подключаются модулями**.
Связи и роли объявлены один раз в ядре; доменные типы (ИБ, ИТ, следующий домен) —
подтипы родовых и играют те же роли. Поэтому требование любого домена участвует
в одних и тех же связях, а новый домен не требует правки ядра.

## 2. Состав

| модуль | файл | сущностей | связей | атрибутов |
|---|---|---|---|---|
| core | `core.tql` | 21 | 35 | 50 |
| it | `it.tql` | 7 | 0 | 1 |
| sec | `sec.tql` | 11 | 3 | 8 |

Подписки ролей для типов, объявленных в другом модуле: модуль sec: `asset`, `threat`. Это не новые типы модуля — тип объявлен в ядре, модуль только подписывает его на роль.

Итого: **39 сущностей**, **38 связей**,
**59 атрибутов**, **88 ролей в связях**,
**1 подписок plays**.

## 3. Соглашения (важно для аудита)

1. **Идентичность.** У каждой сущности обязательный атрибут `ent-key` вида `<тип>:<sha1(ключ)>`.
   Смысловой ключ собирается из данных документа: документ — «издатель|вид|номер»,
   редакция — документ + версия, пункт — документ + `struct_index`,
   требование — документ + пункт + порядок (или хэш дословного текста),
   мера — нормализованный код (`АВЗ-1` = `АВЗ.1` = `авз 1`), регулятор — каноническое имя.
   Домен входит в ключ, поэтому коллизий между доменами нет.
2. **Провенанс.** Любая сущность помнит источник: `source-chunk-id` (первый фрагмент),
   `source-chunk-ids` (все фрагменты), `source-clause` (пункт). Это позволяет показать
   цитату для каждого факта и применить права доступа по источнику.
3. **Обязательность.** В TypeDB 3 `@abstract` не навязывает контракт подтипам, поэтому
   обязательные атрибуты (`@key`, `@card(1..1)`) дополнительно проверяются конвейером записи:
   сущность без обязательного атрибута не пишется и попадает в отчёт «отброшено».
4. **Роли.** Каждая роль связи объявлена с кардинальностью; роли `@card(0..)` необязательны.
   Исполнитель роли указывается явно (`entity X plays relation:role`) — если роль без
   исполнителя, TypeDB отвергает вставку всей связи ([INF11]), поэтому такие ошибки проверяются.
5. **Единицы домена.** Доменные признаки добавляются подтипами и владением, но не дублируют
   родовые типы: `cii_object` и `security_software` — подтипы `asset`, `certificate` — подтип
   `evidence`, `threat_source` — подтип `threat`, `vulnerability` — родовой тип ядра с
   доменными атрибутами БДУ.

## 4. Ядро: родовые типы

Родовые понятия доменно-независимы: их используют и ИБ, и ИТ, и любой следующий домен.

### 4.1 Сущности ядра

| тип | по-русски | родитель | @abstract | атрибуты (owns) | роли (plays) |
|---|---|---|---|---|---|
| `identified_entity` | любой объект с постоянным ключом (основа всех типов) | — | да | `ent-key` **@key** | — |
| `sourced_entity` | объект с происхождением: чанк, пункт, документ-источник | `identified_entity` | да | `source_origin`, `source-chunk-id`, `source-chunk-ids` @card(0..), `source-clause` | — |
| `document` | документ: закон, приказ, стандарт или документ компании | `sourced_entity` | да | `title` @card(1..1), `doc_type`, `document_key` **@key** | — |
| `version` | редакция документа (версия со статусом и хешем текста) | `sourced_entity` | да | `version_string` @card(1..1), `version_status` @card(1..1), `version_hash` @unique, `effective_from`, `effective_to`, `kag_document_id` | — |
| `clause` | структурный элемент документа: раздел, пункт, приложение | `sourced_entity` | да | `struct_index` @card(1..1), `text_content` | — |
| `requirement` | требование: что должно быть сделано | `sourced_entity` | да | `requirement_key` **@key**, `requirement-id` @card(1..1), `requirement_kind` @card(1..1), `description`, `obligation-level` | — |
| `control` | мера или контроль: чем требование выполняется | `sourced_entity` | да | `measure_key` **@key**, `measure-category`, `code`, `description` | — |
| `evidence` | доказательство выполнения: сертификат, акт, протокол | `sourced_entity` | да | `name` @card(1..1), `evidence_kind`, `evidence_date` | — |
| `party` | субъект: организация, орган, человек, роль-исполнитель | `sourced_entity` | — | `name` @card(1..1), `party_kind` | — |
| `asset` | актив: система, объект, оборудование, ПО, услуга | `sourced_entity` | — | `name` @card(1..1), `asset_kind`, `criticality` | — |
| `data_object` | объект данных: набор сведений, обрабатываемый системой | `sourced_entity` | — | `name` @card(1..1), `data_kind` | — |
| `data_category` | категория данных: ПДн, КТ, ГТ, коммерческая тайна | `sourced_entity` | — | `name` @card(1..1) | — |
| `threat` | угроза: источник или событие, способное нанести ущерб | `sourced_entity` | — | `name` @card(1..1), `threat_kind` | — |
| `vulnerability` | уязвимость: слабое место, через которое действует угроза | `sourced_entity` | — | `name` @card(1..1) | — |
| `risk` | риск: сочетание угрозы, уязвимости и ценности актива | `sourced_entity` | — | `name` @card(1..1), `risk_level`, `likelihood`, `impact` | — |
| `incident` | инцидент: произошедшее событие с ущербом | `sourced_entity` | — | `name` @card(1..1), `incident_date`, `incident_kind` | — |
| `violation` | нарушение: невыполненное требование или отсутствующая мера | `sourced_entity` | — | `name` @card(1..1), `violation_kind` | — |
| `term` | термин с определением (словарная статья в онтологии) | `sourced_entity` | — | `name` @card(1..1), `definition` | — |
| `business_function` | функция: что организация делает (оценка рисков, аудит) | `sourced_entity` | — | `name` @card(1..1) | — |
| `business_process` | процесс: как функция выполняется | `sourced_entity` | — | `name` @card(1..1) | — |
| `actor_role` | роль исполнителя: кто отвечает за действие | `sourced_entity` | — | `name` @card(1..1) | — |

### 4.2 Связи ядра

| связь | по-русски | родитель | роли (relates) | атрибуты связи |
|---|---|---|---|---|
| `document_issuance` | документ издан органом (регулятором) | — | `issuer` @card(1..1), `regulated_item` @card(1..1) | — |
| `document_has_version` | у документа есть редакция | — | `document` @card(1..1), `version` @card(1..1) | — |
| `current_version_of` | редакция действует сейчас | — | `document` @card(1..1), `version` @card(1..1) | — |
| `supersedes` | редакция заменяет предыдущую | — | `newer` @card(1..1), `older` @card(1..1) | — |
| `document_structure` | редакция содержит структурный элемент | — | `version` @card(1..1), `element` @card(1..1) | — |
| `document_hierarchy` | элемент вложен в другой элемент (раздел → пункт) | — | `parent` @card(1..1), `child` @card(1..1) | — |
| `requirement_derived_from` | требование выведено из пункта документа | — | `requirement` @card(1..1), `clause` @card(1..1) | — |
| `requirement_definition` | мера определяет требование (что именно выполняется) | — | `requirement` @card(1..1), `measure` @card(1..1) | `point-in-measure` |
| `measure_source` | мера описана в пункте документа | — | `measure` @card(1..1), `clause` @card(1..1) | — |
| `measure_detailing` | подмера детализирует меру (АВЗ.1.1 → АВЗ.1) | — | `submeasure` @card(1..1), `parent_measure` @card(1..1) | — |
| `compensatory_relation` | одна мера компенсирует другую | — | `core_measure` @card(1..1), `alternative_measure` @card(1..1) | — |
| `requirement_applicability` | требование относится к объекту | — | `requirement` @card(1..1), `target_system` @card(1..1) | — |
| `measure_applicability` | мера применяется к объекту | — | `measure` @card(1..1), `target_system` @card(1..1) | — |
| `requirement_evidence` | требование подтверждается доказательством | — | `requirement` @card(1..1), `evidence` @card(1..1) | — |
| `requirement_basis` | требование опирается на документ-основание | — | `requirement` @card(1..1), `source_document` @card(1..1) | — |
| `requirement_coverage` | документ компании покрывает требование | — | `covered_requirement` @card(1..1), `covering_document` @card(1..1) | `coverage-status` |
| `document_compliance` | документ компании соответствует нормативному | — | `company_doc` @card(1..1), `regulated_doc` @card(1..1) | — |
| `document_assignment` | документ распространяется на субъект | — | `regulated_doc` @card(1..1), `regulated_subject` @card(1..1) | — |
| `alignment` | соответствие между доменами (мера ИБ ↔ ИТ-контроль) | — | `alignment_source` @card(1..1), `alignment_target` @card(1..1) | `alignment-kind`, `confidence`, `authored-by`, `source-clause` |
| `asset_ownership` | у актива есть владелец | — | `owner` @card(1..1), `owned_asset` @card(1..1) | — |
| `asset_part_of` | актив входит в состав другого актива | — | `part` @card(1..1), `whole` @card(1..1) | — |
| `data_processing` | субъект обрабатывает данные | — | `processor` @card(1..1), `processed_data` @card(1..) | — |
| `data_classification` | данные отнесены к категории | — | `classified_data` @card(1..1), `category` @card(1..1) | — |
| `responsibility` | субъект отвечает за требование, меру или процесс | — | `responsible` @card(1..1), `duty_requirement` @card(0..), `duty_control` @card(0..), `duty_process` @card(0..), `duty_role` @card(0..) | — |
| `risk_composition` | из чего сложился риск: угроза, уязвимость, актив | — | `risk` @card(1..1), `risk_threat` @card(0..), `risk_vulnerability` @card(0..), `risk_asset` @card(0..) | — |
| `threat_exploits` | угроза использует уязвимость | — | `threat` @card(1..1), `exploited_vulnerability` @card(1..) | — |
| `threat_target` | угроза направлена на актив | — | `threat` @card(1..1), `target_asset` @card(1..) | — |
| `incident_impact` | инцидент затронул актив | — | `incident` @card(1..1), `affected_asset` @card(0..), `incident_threat` @card(0..1) | — |
| `function_execution` | функцию выполняет субъект | — | `performed_function` @card(1..1), `performer` @card(1..), `used_control` @card(0..) | — |
| `document_reference` | документ ссылается на другой документ | — | `referring` @card(1..1), `referenced` @card(1..1) | `source-clause` |
| `control_mitigation` | мера снижает угрозу | — | `mitigating_control` @card(1..1), `mitigated_threat` @card(1..) | `confidence` |
| `violation_of` | — | — | `violation` @card(1..1), `violated_requirement` @card(0..), `missing_control` @card(0..), `violating_document` @card(0..), `affected_object` @card(0..), `detected_evidence` @card(0..) | — |
| `term_source` | — | — | `defined_term` @card(1..1), `defining_clause` @card(1..1) | — |
| `requirement_succession` | — | — | `newer_requirement` @card(1..1), `older_requirement` @card(1..1) | `alignment-kind`, `confidence` |
| `clause_reference` | — | — | `referring_clause` @card(1..1), `referenced_clause` @card(1..1) | `source-clause` |

### 4.3 Атрибуты ядра

| атрибут | тип значения |
|---|---|
| `source_origin` | string |
| `source-chunk-id` | string |
| `source-chunk-ids` | string |
| `source-clause` | string |
| `name` | string |
| `short_name` | string |
| `title` | string |
| `number` | string |
| `doc_type` | string |
| `document_key` | string |
| `version_string` | string |
| `version_status` | string |
| `version_hash` | string |
| `kag_document_id` | string |
| `effective_from` | date |
| `effective_to` | date |
| `struct_index` | string |
| `text_content` | string |
| `requirement-id` | string |
| `requirement_key` | string |
| `requirement_kind` | string |
| `obligation-level` | string |
| `description` | string |
| `measure_key` | string |
| `measure-category` | string |
| `code` | string |
| `point-in-measure` | string |
| `alignment-kind` | string |
| `confidence` | double |
| `authored-by` | string |
| `cert_number` | string |
| `cert_authority` | string |
| `cert_until` | date |
| `evidence_kind` | string |
| `evidence_date` | date |
| `vendor` | string |
| `software_version` | string |
| `party_kind` | string |
| `asset_kind` | string |
| `criticality` | string |
| `data_kind` | string |
| `threat_kind` | string |
| `risk_level` | string |
| `likelihood` | string |
| `impact` | string |
| `incident_date` | date |
| `incident_kind` | string |
| `violation_kind` | string |
| `definition` | string |
| `coverage-status` | string |

## 5.1 Домен `it`

### Сущности

| тип | по-русски | родитель | @abstract | атрибуты (owns) | роли (plays) |
|---|---|---|---|---|---|
| `it_document` | документ домена ИТ (регламент, инструкция) | `document` | — | — | — |
| `it_standard` | стандарт домена ИТ (например, ISO 27001, Cobit) | `document` | — | — | — |
| `it_requirement` | требование ИТ-контура | `requirement` | — | — | — |
| `it_control` | ИТ-контроль (AC-2, AU-6) | `control` | — | `control-family` | — |
| `it_service` | ИТ-услуга | `asset` | — | `service-criticality` | — |
| `it_asset` | ИТ-актив: сервер, сеть, рабочая станция | `asset` | — | `vendor`, `software_version` | — |
| `it_configuration_item` | единица конфигурации (CI) в ITSM | `asset` | — | `description` | — |

### Атрибуты

| атрибут | тип значения |
|---|---|
| `control-family` | string |

## 5.2 Домен `sec`

### Сущности

| тип | по-русски | родитель | @abstract | атрибуты (owns) | роли (plays) |
|---|---|---|---|---|---|
| `regulator` | регулятор: ФСТЭК, ФСБ, Банк России, Минцифры | `party` | — | `short_name` | `document_issuance:issuer` |
| `regulatory_document` | нормативный документ регулятора | `document` | — | `number` | — |
| `company_document` | документ компании (политика, приказ, регламент) | `document` | — | — | — |
| `document_version` | редакция нормативного документа | `version` | — | — | — |
| `structural_element` | пункт нормативного документа | `clause` | — | — | — |
| `security_requirement` | требование информационной безопасности | `requirement` | — | — | — |
| `security_measure` | мера защиты информации (АВЗ.1, ЗНИ.2) | `control` | — | — | — |
| `cii_object` | объект критической информационной инфраструктуры | `asset` | — | `cii-category` @card(1..1), `system_type` | — |
| `security_software` | средство защиты информации | `asset` | — | `vendor`, `software_version` | — |
| `certificate` | сертификат соответствия на СЗИ | `evidence` | — | `cert_number` **@key**, `cert_authority`, `cert_until` | — |
| `threat_source` | источник угрозы: класс нарушителя и его возможности | `threat` | — | `offender_class`, `potential` | — |

### Связи

| связь | по-русски | родитель | роли (relates) | атрибуты связи |
|---|---|---|---|---|
| `certification` | сертификат подтверждает соответствие СЗИ требованию | — | `certified_software` @card(1..1), `certificate` @card(1..1), `basis_requirement` @card(0..1) | — |
| `asset_protection` | СЗИ защищает объект | — | `protected_system` @card(1..1), `deployed_szi` @card(1..1) | — |
| `threat_applicability` | угроза актуальна для объекта | — | `threat` @card(1..1), `target_system` @card(1..1) | — |

### Атрибуты

| атрибут | тип значения |
|---|---|
| `system_type` | string |
| `offender_class` | string |
| `potential` | string |
| `bdu-id` | string |
| `severity-level` | integer |
| `cvss-score` | double |
| `published-date` | date |
| `affected-software` | string |

## 6. Как схема собирается и загружается

    python -m src.ontology.build_schema --db <база> --modules sec,it

Сборщик конкатенирует ядро и выбранные модули, проверяет:
- в ядре нет доменных имён (`sec_`, `it_`, `security`, `cii_`, `bdu`);
- нет двойных объявлений одного типа;
- каждая роль связи имеет исполнителя (проверка отдельно, см. п. 3.4).
Затем загружает схему одной транзакцией и печатает счётчики типов.

Доступные базы: `cybersecurity_ontology_v26` (актуальная схема, 128 типов сущностей,
35 связей, 62 атрибута, 77 ролей), `v23` (рабочие данные предыдущей схемы), `v25`, `v24` (отладка).

## 7. Что известно про качество заполнения (на 15.11.2026)

Документ `fstek-2014-mery-zashchity-gis.pdf`, 321 фрагмент, прогон на схеме v23:

| показатель | значение |
|---|---|
| пунктов документа | 195 |
| требований | 473 |
| мер | 114 |
| требований со связью «выведено из пункта» | 466 (98,5 %) |
| требований со связью «мера определяет требование» | 351 (74 %) |
| связей «мера описана в пункте» | 130 |
| всего связей | 1174 |

Ограничение: обработан один документ из корпуса; документы компании не обрабатывались,
поэтому ветка покрытия (`requirement_coverage`, `document_compliance`) не заполнена.

## 8. Что заведомо не покрыто (кандидаты на доработку после аудита)

1. Таблицы как объекты (строка, ячейка) — таблица мер извлекается как текст, а не как структура.
2. Приложения и формы (документ → приложение → таблица).
3. Перекрёстные ссылки «пункт → пункт» внутри документа (есть только документ → документ).
4. Связь «термин → пункт, где он определён».
5. Версии требования между редакциями документа.
6. Матрица «требование × роль исполнителя» (типы есть, связи нет).
7. Правила TypeDB (`when → then`) — сейчас онтология является графом фактов без вывода.
8. Второй домен (ИТ) — только схема, данных нет.

## 9. Вопросы, на которые нужен ответ аудита

1. Достаточна ли модель «ядро + доменные модули» для нескольких предметных областей,
   или нужна другая декомпозиция (например, отдельные базы с федеративным слоем)?
2. Правильно ли выбрана идентичность сущностей (структурный ключ + хеш текста для требований)?
   Где она сломается на перефразировании, OCR-ошибках, переиздании документа?
3. Достаточно ли связей, чтобы отвечать на вопросы вида «какие требования предъявляются
   к объекту», «чем закрывается требование», «что не покрыто документом компании»?
4. Нужны ли правила вывода (TypeDB rules) и какие именно — или вывод должен остаться в коде?
5. Как правильно моделировать таблицы и приложения, не потеряв дословность?
6. Что в схеме лишнее или преждевременное?

## 9.1. Реакция на внешний аудит

Аудит схемы выполнен внешней моделью; решения по каждому замечанию (принято / отклонено / отложено)
зафиксированы в `docs/ontology/AUDIT-RESPONSE.md`. Кратко: принято 13 замечаний, из них закрыты
в схеме — ИТ-домен как подтипы `asset`, `regulator sub party`, `bdu-id @unique`, удаление дубля
`threat_measure`, расширение ролей применимости, новые связи `violation_of`, `term_source`,
`requirement_succession`, `clause_reference`, роль `duty_role` в `responsibility`.

## 10. Полный исходный текст схемы

Ниже — файлы в том виде, в котором они загружаются.


### `src/ontology/core.tql`

```tql
define
# =====================================================================
#  ЯДРО ОНТОЛОГИИ KAG — доменно-независимые типы и связи.
#
#  Правила ядра (ADR-010):
#   * в этом файле НЕТ доменных имён (sec_, it_, security, cii, bdu): домен живёт
#     в модулях src/ontology/modules/<домен>.tql и проявляется префиксом в имени типа;
#   * связи и роли объявлены один раз здесь; доменные типы только «играют» роли,
#     поэтому требование любого домена участвует в одних и тех же связях;
#   * ключи содержат домен (sec:авз.1 / it:ac-2), но объявлены здесь — правило одно
#     для всех доменов.
#
#  Ограничение платформы (проверено по docs): в TypeDB 3 @abstract не навязывает
#  подтипам контракт, поэтому обязательность атрибутов дополнительно проверяется
#  конвейером (guard'ы, integrity-функции), а не только схемой.
# =====================================================================

# ---- атрибуты: идентичность и происхождение -------------------------
attribute ent-key, value string;
attribute source_origin, value string;
attribute source-chunk-id, value string;
attribute source-chunk-ids, value string;
attribute source-clause, value string;

# ---- атрибуты: документы и редакции ---------------------------------
attribute name, value string;
attribute short_name, value string;
attribute title, value string;
attribute number, value string;
attribute doc_type, value string;
attribute document_key, value string;
attribute version_string, value string;
attribute version_status, value string;
attribute version_hash, value string;
attribute kag_document_id, value string;
attribute effective_from, value date;
attribute effective_to, value date;

# ---- атрибуты: структура, требования, контроли ----------------------
attribute struct_index, value string;
attribute text_content, value string;
attribute requirement-id, value string;
attribute requirement_key, value string;
attribute requirement_kind, value string;
attribute obligation-level, value string;
attribute description, value string;
attribute measure_key, value string;
attribute measure-category, value string;
attribute code, value string;
attribute point-in-measure, value string;

# ---- атрибуты: выравнивание доменов ---------------------------------
# Связь alignment: exact | close | related + уверенность + автор.
attribute alignment-kind, value string;
attribute confidence, value double;
attribute authored-by, value string;

# ---- атрибуты: доказательства и сертификация ------------------------
attribute cert_number, value string;
attribute cert_authority, value string;
attribute cert_until, value date;
attribute evidence_kind, value string;
attribute evidence_date, value date;

# ---- атрибуты: угрозы и уязвимости ----------------------------------
attribute vendor, value string;
attribute software_version, value string;

# ---- атрибуты: субъекты, активы, данные, риски, инциденты (ядро) -----
# Родовые признаки: доменное уточнение живёт в модуле (например, cii-category — в sec).
attribute party_kind, value string;          # organization | person | regulator | operator | owner | user | admin | auditor
attribute asset_kind, value string;          # information_system | kii_object | gis | ispdn | asu_tp | hardware | software | network | facility | service
attribute criticality, value string;         # low | medium | high | critical
attribute data_kind, value string;           # personal | biometric | confidential | state_secret | commercial | public
attribute threat_kind, value string;         # source | event | impact
attribute risk_level, value string;          # low | medium | high | critical
attribute likelihood, value string;          # rare | unlikely | possible | likely | almost_certain
attribute impact, value string;              # minor | moderate | major | critical
attribute incident_date, value date;
attribute incident_kind, value string;       # leak | failure | attack | error
attribute violation_kind, value string;      # missing_control | outdated_document | unfulfilled_requirement
attribute definition, value string;          # текст определения термина

# ---- происхождение и идентичность -----------------------------------
entity identified_entity @abstract, owns ent-key @key;
entity sourced_entity @abstract, sub identified_entity,
  owns source_origin,
  owns source-chunk-id,
  owns source-chunk-ids @card(0..),
  owns source-clause;

# ---- ДОКУМЕНТ (ядро) ------------------------------------------------
entity document @abstract;
entity document sub sourced_entity,
  owns title @card(1..1),
  owns doc_type,
  owns document_key @key;
entity version @abstract;
entity version sub sourced_entity,
  owns version_string @card(1..1),
  owns version_status @card(1..1),
  owns version_hash @unique,
  owns effective_from,
  owns effective_to,
  owns kag_document_id;

# ---- СТРУКТУРА ДОКУМЕНТА (ядро) -------------------------------------
entity clause @abstract;
entity clause sub sourced_entity,
  owns struct_index @card(1..1),
  owns text_content;

# ---- ТРЕБОВАНИЕ (ядро) ----------------------------------------------
entity requirement @abstract;
entity requirement sub sourced_entity,
  owns requirement_key @key,
  owns requirement-id @card(1..1),
  owns requirement_kind @card(1..1),
  owns description,
  owns obligation-level;

# ---- КОНТРОЛЬ / МЕРА (ядро) -----------------------------------------
# Один тип на все домены: мера ИБ и ИТ-контроль — разные подтипы одного control.
entity control @abstract;
entity control sub sourced_entity,
  owns measure_key @key,
  owns measure-category,
  owns code,
  owns description;

# ---- ДОКАЗАТЕЛЬСТВО (ядро) ------------------------------------------
entity evidence @abstract;
entity evidence sub sourced_entity,
  owns name @card(1..1),
  owns evidence_kind,
  owns evidence_date;

# ---- СУБЪЕКТЫ, АКТИВЫ, ДАННЫЕ, УГРОЗЫ, РИСКИ, ИНЦИДЕНТЫ (ядро) -------
# Родовые понятия без домена: ИБ, ИТ и любой следующий домен используют их напрямую.
# Доменное уточнение — подтипами в модуле (например, threat_source sub threat в sec).
entity party sub sourced_entity,
  owns name @card(1..1),
  owns party_kind;
entity asset sub sourced_entity,
  owns name @card(1..1),
  owns asset_kind,
  owns criticality;
entity data_object sub sourced_entity,
  owns name @card(1..1),
  owns data_kind;
entity data_category sub sourced_entity,
  owns name @card(1..1);
entity threat sub sourced_entity,
  owns name @card(1..1),
  owns threat_kind;
entity vulnerability sub sourced_entity,
  owns name @card(1..1);
entity risk sub sourced_entity,
  owns name @card(1..1),
  owns risk_level,
  owns likelihood,
  owns impact;
entity incident sub sourced_entity,
  owns name @card(1..1),
  owns incident_date,
  owns incident_kind;
entity violation sub sourced_entity,
  owns name @card(1..1),
  owns violation_kind;
entity term sub sourced_entity,
  owns name @card(1..1),
  owns definition;
entity business_function sub sourced_entity,
  owns name @card(1..1);
entity business_process sub sourced_entity,
  owns name @card(1..1);
entity actor_role sub sourced_entity,
  owns name @card(1..1);

# ---- связи: документы и редакции ------------------------------------
relation document_issuance,
  relates issuer @card(1..1),
  relates regulated_item @card(1..1);
relation document_has_version,
  relates document @card(1..1),
  relates version @card(1..1);
relation current_version_of,
  relates document @card(1..1),
  relates version @card(1..1);
relation supersedes,
  relates newer @card(1..1),
  relates older @card(1..1);
relation document_structure,
  relates version @card(1..1),
  relates element @card(1..1);
relation document_hierarchy,
  relates parent @card(1..1),
  relates child @card(1..1);

# ---- связи: требование ↔ структура ↔ контроль ------------------------
relation requirement_derived_from,
  relates requirement @card(1..1),
  relates clause @card(1..1);
relation requirement_definition,
  relates requirement @card(1..1),
  relates measure @card(1..1),
  owns point-in-measure @card(0..1);
relation measure_source,
  relates measure @card(1..1),
  relates clause @card(1..1);
relation measure_detailing,
  relates submeasure @card(1..1),
  relates parent_measure @card(1..1);
relation compensatory_relation,
  relates core_measure @card(1..1),
  relates alternative_measure @card(1..1);
relation requirement_applicability,
  relates requirement @card(1..1),
  relates target_system @card(1..1);
relation measure_applicability,
  relates measure @card(1..1),
  relates target_system @card(1..1);
relation requirement_evidence,
  relates requirement @card(1..1),
  relates evidence @card(1..1);
relation requirement_basis,
  relates requirement @card(1..1),
  relates source_document @card(1..1);
relation requirement_coverage,
  relates covered_requirement @card(1..1),
  relates covering_document @card(1..1),
  # статус покрытия: covered | partial | gap (проверяется конвейером)
  owns coverage-status @card(0..1);
relation document_compliance,
  relates company_doc @card(1..1),
  relates regulated_doc @card(1..1);
relation document_assignment,
  relates regulated_doc @card(1..1),
  relates regulated_subject @card(1..1);

# ---- связи: выравнивание доменов (ключ к мульти-доменности) ---------
relation alignment,
  relates alignment_source @card(1..1),
  relates alignment_target @card(1..1),
  owns alignment-kind @card(1..1),
  owns confidence @card(0..1),
  owns authored-by @card(0..1),
  owns source-clause @card(0..1);

# ---- связи: субъекты, активы, данные, угрозы, риски, инциденты -------
# Роли объявлены один раз на родовые типы: доменные подтипы играют их без правки ядра.
relation asset_ownership,
  relates owner @card(1..1),
  relates owned_asset @card(1..1);
relation asset_part_of,
  relates part @card(1..1),
  relates whole @card(1..1);
relation data_processing,
  relates processor @card(1..1),
  relates processed_data @card(1..);
relation data_classification,
  relates classified_data @card(1..1),
  relates category @card(1..1);
relation responsibility,
  relates responsible @card(1..1),
  relates duty_requirement @card(0..),
  relates duty_control @card(0..),
  relates duty_process @card(0..),
  relates duty_role @card(0..);
relation risk_composition,
  relates risk @card(1..1),
  relates risk_threat @card(0..),
  relates risk_vulnerability @card(0..),
  relates risk_asset @card(0..);
# likelihood/impact/risk_level живут только на risk — иначе два источника истины
relation threat_exploits,
  relates threat @card(1..1),
  relates exploited_vulnerability @card(1..);
relation threat_target,
  relates threat @card(1..1),
  relates target_asset @card(1..);
relation incident_impact,
  relates incident @card(1..1),
  relates affected_asset @card(0..),
  relates incident_threat @card(0..1);
relation function_execution,
  relates performed_function @card(1..1),
  relates performer @card(1..),
  relates used_control @card(0..);
relation document_reference,
  relates referring @card(1..1),
  relates referenced @card(1..1),
  owns source-clause @card(0..1);
relation control_mitigation,
  relates mitigating_control @card(1..1),
  relates mitigated_threat @card(1..),
  owns confidence @card(0..1);
# ── связи, которых не хватало по итогам аудита ──────────────────────────────
relation violation_of,
  relates violation @card(1..1),
  relates violated_requirement @card(0..),
  relates missing_control @card(0..),
  relates violating_document @card(0..),
  relates affected_object @card(0..),
  relates detected_evidence @card(0..);
relation term_source,
  relates defined_term @card(1..1),
  relates defining_clause @card(1..1);
relation requirement_succession,
  relates newer_requirement @card(1..1),
  relates older_requirement @card(1..1),
  owns alignment-kind @card(0..1),
  owns confidence @card(0..1);
relation clause_reference,
  relates referring_clause @card(1..1),
  relates referenced_clause @card(1..1),
  owns source-clause @card(0..1);

# ---- роли: играют АБСТРАКТНЫЕ типы, поэтому любой домен подходит -----
# документ в роли «объект издания»: без этой подписки TypeDB не выводит тип
# участника и отвергает всю вставку связи ([INF11] при записи document_issuance).
entity document plays document_issuance:regulated_item;
entity document plays document_has_version:document;
entity version plays document_has_version:version;
entity document plays current_version_of:document;
entity version plays current_version_of:version;
entity version plays supersedes:newer;
entity version plays supersedes:older;
entity version plays document_structure:version;
entity clause plays document_structure:element;
entity clause plays document_hierarchy:parent;
entity clause plays document_hierarchy:child;
entity requirement plays requirement_derived_from:requirement;
entity clause plays requirement_derived_from:clause;
entity requirement plays requirement_definition:requirement;
entity control plays requirement_definition:measure;
entity control plays measure_source:measure;
entity clause plays measure_source:clause;
entity control plays measure_detailing:submeasure;
entity control plays measure_detailing:parent_measure;
entity control plays compensatory_relation:core_measure;
entity control plays compensatory_relation:alternative_measure;
entity requirement plays requirement_applicability:requirement;
entity control plays measure_applicability:measure;
entity requirement plays requirement_evidence:requirement;
entity evidence plays requirement_evidence:evidence;
entity requirement plays requirement_basis:requirement;
entity document plays requirement_basis:source_document;
entity document plays requirement_coverage:covering_document;
entity requirement plays requirement_coverage:covered_requirement;
entity document plays document_compliance:company_doc;
entity document plays document_compliance:regulated_doc;
entity document plays document_assignment:regulated_doc;
entity requirement plays alignment:alignment_source;
entity requirement plays alignment:alignment_target;
entity control plays alignment:alignment_source;
entity control plays alignment:alignment_target;
entity document plays alignment:alignment_source;
entity document plays alignment:alignment_target;

# ---- атрибут статуса покрытия ---------------------------------------
attribute coverage-status, value string;

# ---- роли: субъекты, активы, данные, угрозы, риски, инциденты --------
entity party plays asset_ownership:owner;
entity party plays responsibility:responsible;
entity party plays data_processing:processor;
entity party plays function_execution:performer;
entity asset plays asset_ownership:owned_asset;
entity asset plays asset_part_of:part;
entity asset plays asset_part_of:whole;
entity asset plays threat_target:target_asset;
entity asset plays incident_impact:affected_asset;
entity asset plays risk_composition:risk_asset;
entity data_object plays data_processing:processed_data;
entity data_object plays data_classification:classified_data;
entity data_category plays data_classification:category;
entity threat plays threat_exploits:threat;
entity threat plays threat_target:threat;
entity threat plays risk_composition:risk_threat;
entity threat plays incident_impact:incident_threat;
entity threat plays control_mitigation:mitigated_threat;
entity vulnerability plays threat_exploits:exploited_vulnerability;
entity vulnerability plays risk_composition:risk_vulnerability;
entity risk plays risk_composition:risk;
entity incident plays incident_impact:incident;
entity business_function plays function_execution:performed_function;
entity business_process plays responsibility:duty_process;
entity document plays document_reference:referring;
entity document plays document_reference:referenced;
entity requirement plays responsibility:duty_requirement;
entity control plays responsibility:duty_control;
entity control plays function_execution:used_control;
entity control plays control_mitigation:mitigating_control;

# ---- роли новых связей (аудит 15.11.2026) ----------------------------
entity violation plays violation_of:violation;
entity requirement plays violation_of:violated_requirement;
entity control plays violation_of:missing_control;
entity document plays violation_of:violating_document;
entity asset plays violation_of:affected_object;
entity evidence plays violation_of:detected_evidence;
entity term plays term_source:defined_term;
entity clause plays term_source:defining_clause;
entity requirement plays requirement_succession:newer_requirement;
entity requirement plays requirement_succession:older_requirement;
entity clause plays clause_reference:referring_clause;
entity clause plays clause_reference:referenced_clause;
entity actor_role plays responsibility:duty_role;
# применимость требования и меры — на родовой актив, а не только на объект КИИ (аудит P0-2)
entity asset plays requirement_applicability:target_system;
entity asset plays measure_applicability:target_system;
entity party plays document_assignment:regulated_subject;
entity asset plays document_assignment:regulated_subject;
```


### `src/ontology/modules/it.tql`

```tql
define
# =====================================================================
#  МОДУЛЬ ДОМЕНА: it — информационные технологии.
#
#  Минимальный модуль: требование, контроль, сервис, актив, конфигурационная
#  единица. Домен проявляется префиксом в имени типа; связей и ролей здесь нет —
#  они в ядре, поэтому ИТ-требование участвует в тех же связях, что требование ИБ.
#  Ключи: it:<код> для контролей, «<документ>#<пункт>#<порядок>» для требований.
# =====================================================================

# ---- доменные атрибуты ----------------------------------------------
attribute service-criticality, value string;   # low | medium | high | critical
attribute control-family, value string;        # например: access-control, logging

# ---- документы домена ------------------------------------------------
entity it_document sub document;
entity it_standard sub document;

# ---- требования и контроли домена ------------------------------------
entity it_requirement sub requirement;
entity it_control sub control,
  owns control-family;

# ---- объекты ИТ -------------------------------------------------------
# name наследуется от родового asset — здесь только доменные признаки
entity it_service sub asset,
  owns service-criticality;
entity it_asset sub asset,
  owns vendor,
  owns software_version;
entity it_configuration_item sub asset,
  owns description;
```


### `src/ontology/modules/sec.tql`

```tql
define
# =====================================================================
#  МОДУЛЬ ДОМЕНА: sec — информационная безопасность.
#
#  Здесь только то, что принадлежит домену: конкретные типы (подтипы ядра),
#  доменные атрибуты и доменные связи. Ядро не правится.
#  Имена типов сохранены историческими (security_requirement, security_measure),
#  чтобы существующий путь записи и запросы работали без миграции кода.
# =====================================================================

# ---- доменные атрибуты ----------------------------------------------
attribute cii-category, value integer;
attribute system_type, value string;

# ---- доменные атрибуты: угрозы и уязвимости --------------------------
attribute offender_class, value string;
attribute potential, value string;
attribute bdu-id, value string;
attribute severity-level, value integer;
attribute cvss-score, value double;
attribute published-date, value date;
attribute affected-software, value string;

# ---- документы и органы ---------------------------------------------
# regulator — субъект домена: name наследуется от party, здесь только своё
entity regulator sub party,
  owns short_name,
  plays document_issuance:issuer;

entity regulatory_document sub document,
  owns number;

entity company_document sub document;

entity document_version sub version;

# ---- структура -------------------------------------------------------
entity structural_element sub clause;

# ---- требования и меры домена (подтипы ядра) -------------------------
entity security_requirement sub requirement;
entity security_measure sub control;

# ---- объекты, ПО, сертификаты, угрозы --------------------------------
# Объект КИИ и СЗИ — активы домена ИБ: подтипы родового asset из ядра,
# поэтому автоматически участвуют в asset_ownership, asset_part_of, threat_target.
entity cii_object sub asset,
  owns cii-category @card(1..1),
  owns system_type;

entity security_software sub asset,
  owns vendor,
  owns software_version;

# Сертификат — доказательство: подтип родового evidence из ядра.
entity certificate sub evidence,
  owns cert_number @key,
  owns cert_authority,
  owns cert_until;

# Источник угрозы (нарушитель) — подтип родового threat из ядра.
entity threat_source sub threat,
  owns offender_class,
  owns potential;

# Уязвимость — родовой тип ядра; доменные признаки (БДУ, CVSS) добавляются владением,
# а не вторым типом с тем же именем.
vulnerability owns bdu-id @unique,
  owns description,
  owns severity-level,
  owns cvss-score,
  owns published-date,
  owns affected-software @card(0..);


# ---- доменные связи ---------------------------------------------------
relation certification,
  relates certified_software @card(1..1),
  relates certificate @card(1..1),
  relates basis_requirement @card(0..1);
relation asset_protection,
  relates protected_system @card(1..1),
  relates deployed_szi @card(1..1);
relation threat_applicability,
  relates threat @card(1..1),
  relates target_system @card(1..1);

# ---- роли доменных типов ---------------------------------------------
# применимость требования/меры к активу и назначение документа объявлены в ядре
# (типы родовые) — здесь только доменные подписки.
entity cii_object plays asset_protection:protected_system;
entity asset plays threat_applicability:target_system;
entity security_software plays asset_protection:deployed_szi;
entity security_software plays certification:certified_software;
entity certificate plays certification:certificate;
entity security_requirement plays certification:basis_requirement;
entity threat plays threat_applicability:threat;
```
