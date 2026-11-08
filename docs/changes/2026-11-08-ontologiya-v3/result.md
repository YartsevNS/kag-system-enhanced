# Результат — 08.11.2026

**Онтология v3:** `src/ontology/schema-v3.tql` + `src/ontology/functions-v3.tql`,
развёрнута и проверена на базе `cybersecurity_ontology_v3`.

**Промпт v3:** версия 6 в ontology_prompts (7722 знака), заводской текст обновлён.

Проверено (база cybersecurity_ontology_v3, тестовые данные, source_origin=probe-v3):

    сущностей с наследниками:      139
    документ без title:            отвергнут
    требование без requirement-id: отвергнут
    дубль document_key:            отвергнут
    violated_certification:        1 факт (СЗИ без сертификата)
    после выдачи сертификата:      0 фактов
    violated_documents:            1 факт
    current_versions:              «2026» при supersedes(2026 → 2018)

Ключевой дефект, найденный проверкой: `document_key` без `@key` пропускал дубли —
исправлено в схеме, повторный прогон дубль отверг.

База `cybersecurity_ontology_v2` не тронута и остаётся рабочей до переключения конфига.

## Следующий шаг (блокирует переключение)

1. `typedb_writer.py` под v3: типы security_requirement, evidence, security_software,
   certificate, document_version; связи requirement_definition, measure_source,
   document_structure, requirement_derived_from, requirement_applicability,
   measure_applicability, certification, threat_*, document_compliance, current_version_of.
2. Миграция накопленного (885 structural_element, 142 measure_definition, 316 applicability_criteria)
   либо переобработка корпуса в чистую v3 — решается после writer.
3. Переключение `TYPEDB_DB=cybersecurity_ontology_v3` только после зелёного прогона на 2–3 документах.
