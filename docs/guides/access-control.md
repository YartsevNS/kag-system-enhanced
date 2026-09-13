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

## ACL документа применяется ко всем чтениям (2026-09-13, образ 2026.09.13.26)

Симптом: ACL (`visibility` + `allow/deny`) применялся ТОЛЬКО в фильтре списка
документов. Отдельные эндпоинты документа — `/details`, `/chunks`, `/preview`,
`/thumbnail`, `/status`, `/versions`, `/diff`, `/ocr`, `/ocr/view`, `/tables`,
`/tables/search`, `/access` — отдавали содержимое и метаданные любому
авторизованному пользователю: приватный документ читался по прямой ссылке, минуя
список. Middleware даёт только аутентификацию («кто-то залогинен»), прав на
документ он не знает — и в коде не было НИ ОДНОГО helper-а проверки доступа.

Как сделано: `src/api/services/document_access.py` — единая логика (та же, что в
фильтре списка): deny имеет приоритет, `restricted` доступен только по allow,
legacy `group_ids` учитываются; добавлены владелец (видит свой документ всегда) и
админ. Подключена ко всем чтениям через `ensure_can_read(document_id, user)`
(404 — нет документа, 403 — нет прав), для операций — `ensure_owner_or_admin`.

Заодно по тому же аудиту `upload.py`:

- массовые операции (`/reanalyze-all`, `/reindex-all`, `/reprocess-pending`,
  `/{id}/reprocess-ocr`) были доступны любому авторизованному → только админ;
- `/{id}/reindex` не проверял владельца (у `/{id}/process` проверка была) → теперь
  как у процесса; переиндексация чужого документа не-админом запрещена;
- `/process` при `owner=None` коротко замыкал проверку (`if owner and ...`) и
  пропускал не-админа к системному документу → явные 401/403;
- TUS: `HEAD`/`PATCH`/`DELETE` не проверяли владельца сессии — чужой `upload_id`
  можно было опросить и удалить; плюс TUS обходил блокировку загрузки (теперь
  `_deny_if_uploads_blocked()` в создании сессии);
- `_cached`: в ключ попадал объект `User` (у моделей нет своего `__repr__`, значит
  адрес памяти — после сборки мусора адрес переиспользуется, ключи «слипаются»),
  а размер не ограничивался; ключ нормализован (id вместо объекта), добавлен
  `_CACHE_MAX`;
- `_parse_id_list` сведён к единой реализации в `document_access` (была скопирована
  в трёх местах).

Проверка на стенде (не-админский токен + документ, временно переведённый в
`restricted`): не-админ получает 403 на всех 12 чтениях и на операциях, админ —
200; после возврата в `public` не-админ снова видит документ. TUS: своя сессия 200,
чужая — 403 на HEAD/PATCH/DELETE, при включённой блокировке загрузки создание
сессии — 423 `UPLOADS_BLOCKED`. Аноним на `/upload/list` — 401.

Тесты: `tests/test_document_access.py` (логика ACL: public/restricted/deny/allow,
группы, владелец, админ) и `tests/test_upload_route_guards.py` (права реально
подключены к эндпоинтам + любое имя из `Depends(...)` импортировано в шапке —
на этих граблях api один раз ушёл в crash-loop).
