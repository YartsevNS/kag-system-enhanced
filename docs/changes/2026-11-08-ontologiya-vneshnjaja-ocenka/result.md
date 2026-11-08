# Результат — 08.11.2026

Внешняя оценка сверена с живой схемой по каждому пункту; 10 из 12 замечаний подтвердились,
2 относятся к старой версии файла (`@key` уже стоит, синтаксис применяется).

Применено аддитивно (version_id `c38b283a64244155adfcfdded8ee045f`):

    document_structure, requirement_derived_from, requirement_definition,
    requirement_applicability, threat_applicability, document_compliance,
    current_version_of, certification, entity certificate

Схема: 11 → 12 сущностей, 11 → 19 связей, 31 → 34 атрибута, 75 → 104 типа.
Правила нарушений продолжают работать: `violated_certification()` → «АСУ ТП цеха № 1 / п. 11.1».

Следующие шаги (не выполнены, требуют миграции данных):

1. Нормализовать `document_key` (23 документа без ключа, есть повторы) → затем `@key`.
2. Перелить 142 связи `measure_definition` в `requirement_definition` + переименовать роль.
3. Заполнить `document_structure` по `source-chunk-id` (885 элементов).
4. Заполнить `requirement_derived_from` и `requirement_applicability`.
5. Убрать `current_version_string` после заполнения `current_version_of`.
6. Обновить промпт извлечения под новые связи.
