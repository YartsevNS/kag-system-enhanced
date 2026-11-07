# Каскадное удаление документа из онтологии (P2.11, техдолг 17)

## Проблема

`delete_document()` чистил Postgres, Qdrant, Neo4j и S3, но не TypeDB. Объекты удалённого
документа продолжали участвовать в выводе: правило `violated_documents()` выдавало факт
нарушения по документу, текста которого в системе уже нет. В базе накопились остатки
удалённой тестовой политики (17 `structural_element`, `company_document`, `cii_object`).

## Решение

`src/indexing/typedb_cleanup.py` → `delete_document_ontology(document_id)`.

Порядок обязателен: TypeDB не позволяет удалить участника связи с обязательной ролью
(`@card(1..1)` → CNT5/COW5), поэтому сначала снимаются связи, затем объекты.

    шаг 1  document_version: document_version_of, supersedes (обе стороны), затем объект
    шаг 2  акты (company_document/regulatory_document): document_issuance,
           document_assignment, document_version_of, затем объект
    шаг 3  элементы: applicability_criteria, document_hierarchy, measure_definition,
           compensatory_relation, requirement_basis, requirement_evidence — затем объект

Реестр (`cii_object`, `security_software`) НЕ удаляется: он приходит из файла реестра,
а не из документа.

Каждый шаг — отдельная транзакция: сбой одного не откатывает остальные.

## Проверка (стенд 192.168.50.18)

    документ 798bf879-44be-46a8-908c-3a985e1b0a64
    до удаления:  4 объекта (structural_element 2, company_document 1, document_version 1)
    DELETE /api/v1/upload/{id} → 200
    после:        0 объектов

## Ограничения

- Объекты, потерявшие документ-источник, но связанные с реестром (`asset_protection`),
  не удаляются — это осознанно: реестр живёт отдельно.
- Удаление не запускается для документов, которых уже нет в Postgres (только через CLI).
