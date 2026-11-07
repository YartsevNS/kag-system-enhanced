# Задачи: версии документов регулятора

## Подготовка

- [x] Папка изменения и ADR-008 — файлы на месте, ссылки между ними есть

## Основная работа

- [x] Схема: `document_version`, `document_version_of`, `supersedes`, 6 атрибутов — применено на стенде, `match $v isa document_version` не падает
- [x] Writer: версия на первом чанке, `document_key` — у обработанного документа 1 версия, связана с актом
- [x] Загрузка: `version_string`, `effective_from`, `supersedes_id` + `VERSION_REQUIRED` — регулятор без версии → 400 (проверено curl)
- [x] Замена: связь `supersedes` — загрузка новой редакции помечает прежнюю; статус вычисляется моделью
- [x] Qdrant payload + фильтр `current` — поля в payload, `include_superseded` отключает отсечение
- [~] Правила `current_versions()` — объявлена и вызывается; итерация по результату не парсится в TypeQL 3 (в продукте не нужна)
- [x] UI: поля версии и даты — без них загрузка регулятора не стартует
- [x] Файлы схемы `project_typedb/adr/` обновлены

## Проверка

- [x] Сквозной тест двумя редакциями — 1 акт, 2 версии, 1 `supersedes`, 1 действующая
- [x] Повторная запись идемпотентна — `skipped: version exists`, число версий не растёт
- [x] ACL — фильтр по `visible_chunk_ids` в `outdated_references`
- [x] Тесты — `pytest tests/test_document_versions.py` → 7 passed
- [ ] Поиск на двух живых документах — отложено: в Qdrant нет `superseded`-чанков

## Закрытие

- [x] `result.md` с цифрами
- [x] строка в `docs/ARCHITECTURE-JOURNAL.md`
- [ ] папка перенесена в `archive/` — оставлена: есть открытые пункты
