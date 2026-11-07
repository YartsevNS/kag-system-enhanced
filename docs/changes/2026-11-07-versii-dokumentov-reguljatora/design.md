# Решение: версии документов регулятора

## Подход

Акт и редакция разделены. `regulatory_document` — идентичность (номер, тип, издатель, `document_key`,
`current_version_string`). `document_version` — файл редакции с провенансом на чанки
(`ent-key = kag_document_id`, поэтому переобработка идемпотентна). Связь `document_version_of`
привязывает редакцию к акту, `supersedes` — цепочка замен. Актуальность — атрибут `version_status`,
а не отдельный тип и не перезапись.

Отвергнуто: хранить версию атрибутом на акте (теряется история и смешивается провенанс чанков);
определять замену по году из названия (ошибается на «с изм. № 1» и «ред. от 05.05.2024»);
отдельная коллекция Qdrant под редакции (фильтра по payload достаточно).

## Затрагиваемые файлы и данные

| Что | Путь / имя | Тип изменения |
|---|---|---|
| схема онтологии | `cybersecurity_ontology_v2` | новые: entity `document_version`, relations `document_version_of`, `supersedes`, атрибуты `effective_from`, `effective_to`, `version_status`, `version_hash`, `kag_document_id`, `document_key`; `owns` у `regulatory_document` |
| файл схемы | `project_typedb/adr/schema-v2.tql`, `functions-v2.tql` | правка (аддитивно) |
| writer | `src/indexing/typedb_writer.py` | версия на первом чанке, `document_key`, статусы |
| загрузка | `src/api/routes/upload.py`, `src/api/services/document_service.py` | поля `version_string`, `effective_from`, `supersedes_id`; валидация `VERSION_REQUIRED` |
| UI | `src/api/static/documents.html` | поля версии/даты, валидация |
| поиск | `src/indexing/embeddings_service.py`, `src/api/services/chat_service.py` | payload + фильтр `current` |
| правила | `functions-v2.tql` | `current_versions()`, `outdated_references()` |
| тесты | `tests/test_document_versions.py` | новые |

## Порядок выполнения

1. Определить типы в схеме и применить на стенде (`define` аддитивно).
2. Writer: создание версии, ключ акта, статусы.
3. Загрузка и UI: поля, валидация, сохранение в `source_metadata`.
4. Qdrant: payload и фильтр по умолчанию.
5. Правила: `current_versions()`, `outdated_references()`.
6. Проверка двумя редакциями, тесты, отчёт.

## Откат

Типы аддитивны — старые правила и объекты работают без них. Откат кода — `git revert` коммита
изменения; объекты `document_version` удаляются запросом по `kag_document_id`, если понадобится
чистая база. База `cybersecurity_ontology` не затрагивается.

## Открытые вопросы

- `document_key` для документов без номера — нормализованный `title` (решение на время выполнения).
- `published_at` (дата публикации) не вводим: в модели достаточно `effective_from`.
