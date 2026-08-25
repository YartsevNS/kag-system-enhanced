# Гид: Права доступа (ACL)

Реализовано 2026-08-24. Управление доступом к документам: запрет/разрешение
конкретным группам и пользователям, «всем запрещено — избранным разрешено».

## Модель
Документ (documents, JSONB-колонки):
- `visibility`: public | restricted
- `allow_group_ids` / `deny_group_ids` — группы
- `allow_user_ids` / `deny_user_ids` — пользователи

Права задаются при **загрузке документа** (форма на странице «Документы»:
visibility + select'ы «Разрешено»/«Запрещено») и редактируются кнопкой
«🔒 Права» в карточке документа. Админка — только пользователи/группы.

## Как работает
1. При индексации чанки наследуют права документа (payload: visibility, allow_*, deny_*).
2. **Pre-filter в Qdrant** (search): доступно, если public ИЛИ группа/пользователь в allow;
   запрещено (must_not), если в deny. is_admin — всё.
3. **Post-guard в chat_service** (_access_guard): 2-й слой перед контекстом LLM.
4. Смена прав у загруженного документа → обновление payload чанков (set_payload,
   без переиндексации): PUT /api/v1/upload/{id}/access.

## API
- GET  /api/v1/upload/access-options — группы и пользователи (для формы)
- GET  /api/v1/upload/{id}/access — права документа
- PUT  /api/v1/upload/{id}/access — сохранить права + payload чанков
- POST /api/v1/upload/ — при загрузке: form-поля visibility, allow_*_ids, deny_*_ids (JSON)

## Файлы
- src/database/document_models.py — колонки ACL
- src/database/migrations.py — _COLUMN_MIGRATIONS
- src/api/services/document_service.py — DocumentRecord, upload_document(access)
- src/api/routes/upload.py — Form-параметры + access-эндпоинты
- src/indexing/embeddings_service.py — payload access, set_document_access, ACL-фильтр в search
- src/api/services/chat_service.py — _access_guard (post-guard)
- src/api/static/documents.html — форма при загрузке + модалка «🔒 Права»

## Ограничения
- Старые чанки (до этой фичи) не имеют ACL-полей → трактуются как public.
  Для restricted нужна переиндексация или set_document_access.
- deny приоритетнее allow (запрет действует даже если есть разрешение).
