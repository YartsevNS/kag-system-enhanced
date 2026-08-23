# Гид: чат с документами

> Актуально на 2026-08-23.

## Архитектура

- **Backend:** `src/api/routes/chat.py`, `src/api/services/chat_service.py`, `src/api/services/chat_storage.py`.
- **Frontend:** `src/api/static/chat.html`.
- **Серверные сессии** (авторизованные): `chat_sessions` / `chat_messages` (SQL, FK users, cascade, проверка владельца).
- **Анонимы:** localStorage fallback (история для LLM из тела запроса).
- **401 для анонимов** на /api/* — by design (security middleware требует JWT).

## Маршрутизация интента

`_detect_meta_intent(query)` в chat_service.py — до RAG:
- **count** («сколько документов») → SQL count, без списка (stats_line).
- **list** («перечисли документы») → SQL список, **limit 25** (для не-админов фильтр по группам).
- **semantic** (всё остальное) → RAG по Qdrant.
- `intent` возвращается в metadata ответа.

**Почему:** deepseek-v4-flash даёт ПУСТОЙ ответ при промпте >~3К токенов (список 60 доков ≈ 3400 токенов) и при max_tokens <500. Фронтенд шлёт max_tokens=2048.

## Источники-документы

- Backend отдаёт `sources` с `document_id` + `filename` (chat.py).
- Frontend показывает список уникальных документов (Map по document_id) с кликабельными ссылками на preview:
  `/api/v1/upload/{document_id}/preview` (новая вкладка).
- Фикс: `const seen = new Map()` объявлена ДО блока `if (sources…)` (иначе ReferenceError при пустых sources).

## Известные ограничения

- flash-модель: пустой ответ при промпте >3К токенов или max_tokens <500.
- `_embedding_client` после рестарта api мог быть None → чат молча отдавал 0 источников (исправлено ленивой автоинициализацией в search()).
- Утечка чатов между пользователями на одном компьютере была (localStorage) — исправлено: чистка localStorage при логине, cookie приоритетнее header, серверные сессии.

## Эндпоинты сессий

- `GET /api/v1/chat/sessions` — список сессий пользователя
- `POST /api/v1/chat/sessions` — создать
- `GET /api/v1/chat/sessions/{id}/messages` — сообщения
- `DELETE /api/v1/chat/sessions/{id}` — удалить
- `POST /api/v1/chat/sessions/{id}/rename` — переименовать
- Экспорт читает из БД.

## Проверка

- «покажи все документы» → list (мета, без RAG)
- «сколько документов загружено» → count («145 документов»)
- «что сказано про кибербезопасность» → semantic (RAG, 10 источников)
- «перечисли документы по ГОСТ» → list
