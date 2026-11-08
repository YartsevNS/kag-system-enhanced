# Второй круг ревью онтологии v3 (08.11.2026)

Источник: разбор в чате DeepSeek (шара rnhsg8zhjbnwtlkywd), 93 пункта. Ниже — что взято,
что применено, что отвергнуто проверкой.

## Взято и применено (подтверждено базой)

| Замечание | Что сделано |
|---|---|
| `document` и `sourced_entity` не `@abstract` — можно создать бессмысленный объект | добавлен `@abstract` (проверено: `insert $x isa sourced_entity` и `isa document` отвергаются) |
| нет связи «документ → ВСЕ его редакции», только текущая; исторические версии — сироты | добавлена `document_has_version` (document → version) |
| `current_version_of` не ограничивает «одна текущая редакция на документ» | ограничение аннотацией невозможно (см. ниже); введена функция `documents_with_multiple_current()` |
| `requirement-id @card(1..1)` не даёт уникальности — дубли требований | введён `requirement_key @key` = `document_key|пункт`, `requirement-id` остался номером для показа |
| `cert_number` без уникальности | `cert_number @key` |
| `code` меры без уникальности | введён `measure_key @key` |
| `violated_certification` не проверяет применимость требования | добавлена `requirement_applicability(requirement, target_system)` в функцию |
| просроченный сертификат считался выполнением требования | функции принимают дату: `violated_certification($today: date)`, `cert_until > $today` |
| `current_versions` считала текущей любую «не заменённую» версию, включая сироту | функция считается через `current_version_of` |
| `structural_element` без текста, `evidence` без имени, `threat_source` без класса нарушителя | `text_content @card(1..1)`, `name @card(1..1)`, `offender_class @card(1..1)` |
| промпт: осталась устаревшая роль `source_requirement`; противоречие про `category`; `basis_requirement` подавался как обязательный | текст промпта исправлен (версия 7) |
| промпт: нет требования глобальной уникальности `ent-key` и нормализации `document_key` | добавлены правила нормализации и самопроверка перед выводом |

## Отвергнуто проверкой (совет неверен для TypeDB 3.14.1)

    "@unique на роли current_version_of / document_hierarchy / document_structure"
      → [ANN5] Annotation '@unique' is not supported for relates.
      Факт получен применением схемы, а не чтением документации.
      Замена: функции целостности documents_with_multiple_current(),
      child_elements_with_multiple_parents(), elements_without_version(),
      versions_without_document().

## Проверено на данных (база cybersecurity_ontology_v3)

    дубль document_key                      → отвергнут
    дубль requirement_key                   → отвергнут
    документ без title                      → отвергнут
    голый document (без подтипа)            → отвергнут
    голый sourced_entity                    → отвергнут
    structural_element без text_content     → отвергнут
    просроченный сертификат (2025-01-01)    → нарушение остаётся
    действующий сертификат (2027-01-01)     → нарушений 0
    все редакции документа                  → 2018 и 2026 (document_has_version)
    current_versions                        → 2026
    documents_with_multiple_current()       → поймала документ с двумя текущими
    expired_certificates(2026-11-08)        → СФ-OLD

## Промпт

Версия 7 (8 822 знака): убрана роль source_requirement, добавлены `requirement_key`,
`measure_key`, `document_has_version`, правило «одна текущая редакция», нормализация
`ent-key`/`document_key` и самопроверка перед выводом. Заводской текст обновлён.

## Что осталось

1. `typedb_writer.py` под v3.1 — блокирует переключение базы.
2. Вызов функций с датой из кода: `violated_certification(2026-11-08)` — сигнатура изменилась,
   `typedb_facts` и `chat_service` пока зовут старую без аргументов.
3. Применимость к категории (а не к конкретному объекту) — ревьюер прав, это следующий уровень:
   сейчас требование привязывается к объекту, для «категория 1» придётся либо перечислять
   объекты, либо вводить класс объектов.
