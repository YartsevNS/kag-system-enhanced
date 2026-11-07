# Путь ввода данных компании (P1.7)

## Проблема

Правила онтологии `violated_certification()` и `violated_documents()` рассчитаны на данные
компании, которых в системе не было ни одного объекта:

    company_document   2 (остатки тестовой политики)
    security_requirement  0
    security_software     0
    cii_object с cii-category  0

Правило не может дать факт, если нет объекта КИИ, развёрнутого СЗИ, требования о
сертификации и связи политики с объектом. Цифра «0 фактов» означала отсутствие данных,
а не ошибку правила.

## Что требуется правилам

`violated_certification()`:

    cii_object с cii-category 1
    security_software с is-certified false
    asset_protection(protected_system, deployed_szi)
    security_requirement с requirement_kind "certification"

`violated_documents()` — то же плюс:

    company_document + document_assignment(regulated_subject, regulated_doc)

## Решение

Реестр компании — JSON-файл, загружается в TypeDB идемпотентно:

    src/indexing/company_registry.py          класс CompanyRegistry
    scripts/import_company_registry.py        CLI, --dry-run
    формат: objects / software / protects / requirements / documents

Особенности:
- каждый элемент — своя транзакция (пачечный коммит откатывает всё — этот дефект уже
  ломал версии документов);
- повторная загрузка ничего не дублирует: объект опознаётся по `ent-key`;
- `is-certified` пишется логическим значением, не строкой;
- `source_origin` в реестре помечает проверочные данные (`test-registry`).

## Отвергнутые варианты

- **Извлекать объекты КИИ из документов компании автоматически.** Политика компании
  описывает требования, а не инвентаризацию: перечня СЗИ и категорий объектов там нет.
  Извлечение даст мусор вместо реестра.
- **Хранить реестр в Postgres и строить онтологию на лету.** Реестр — источник фактов для
  вывода; его место в онтологии, иначе правила не увидят данных.

## Проверка (стенд 192.168.50.18)

    загрузка: objects 2, software 2, requirements 1, protects 2, documents 1, skipped 0
    повтор:   objects 0, software 0, requirements 0, protects 0, documents 0, skipped 7
    violated_certification: 1  ->  АСУ ТП цеха № 1 / п. 11.1
    violated_documents:     1  ->  политика ООО «Ромашка» / п. 11.1

## Ограничения

- Проверочный реестр помечен `source_origin=test-registry`; в рабочей онтологии его быть
  не должно — удаляется вместе с объектами после закрытия проверочного контура.
- Реестр не заводит документы: политика должна быть загружена обычным путём, связь
  добавляется по её `document_key`.
