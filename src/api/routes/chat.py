"""
Маршруты для работы с чатом
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from loguru import logger
import uuid
import json

from src.models import ChatRequest, ChatResponse, ChatMessage
from src.config import get_settings
from src.api.services.chat_service import chat_service
from src.api.services.export_service import export_service
from src.api.middleware.auth_v2 import get_current_user, get_current_user_optional
from src.database.user_models import User

router = APIRouter()
router_export = APIRouter()


# ═══════════════════════════════════════════════════════════════════════
# Сессии чата — серверное хранение (SQL, привязка к пользователю).
# Зачем: раньше сессии жили в localStorage браузера. При 2+ вкладках каждая
# перезаписывала общий localStorage → диалоги терялись. Серверное хранение
# убирает гонку и даёт изоляцию между пользователями (каждый видит своё).
# ═══════════════════════════════════════════════════════════════════════

@router.get("/sessions", summary="Список сессий текущего пользователя")
async def list_sessions(
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    from src.api.services.chat_storage import chat_storage
    if not current_user:
        return {"sessions": []}
    return {"sessions": chat_storage.list_sessions(current_user.id)}


@router.post("/sessions", summary="Создать новую сессию")
async def create_session(
    body: dict = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    from src.api.services.chat_storage import chat_storage
    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    title = (body or {}).get("title", "Новый диалог")
    return chat_storage.create_session(current_user.id, title=title)


@router.get("/sessions/{session_id}/messages", summary="Сообщения сессии")
async def get_session_messages(
    session_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    from src.api.services.chat_storage import chat_storage
    if not current_user:
        return {"messages": []}
    return {"messages": chat_storage.list_messages(current_user.id, session_id)}


@router.delete("/sessions/{session_id}", summary="Удалить сессию")
async def delete_session(
    session_id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    from src.api.services.chat_storage import chat_storage
    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    ok = chat_storage.delete_session(current_user.id, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return {"status": "ok"}


@router.post("/sessions/{session_id}/rename", summary="Переименовать сессию")
async def rename_session(
    session_id: str,
    body: dict = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    from src.api.services.chat_storage import chat_storage
    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    title = (body or {}).get("title", "")
    ok = chat_storage.rename_session(current_user.id, session_id, title)
    if not ok:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    return {"status": "ok"}


@router.post("/", response_model=ChatResponse, summary="Отправить сообщение в чат")
async def send_message(
    request: ChatRequest,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Отправить сообщение в чат и получить ответ от LLM с RAG.

    - **messages**: Список сообщений (роль + содержимое)
    - **session_id**: Идентификатор сессии (опционально)
    - **stream**: Включить потоковую передачу (опционально)
    - **temperature**: Температура генерации (0.0-1.0)
    - **max_tokens**: Максимальное количество токенов

    Возвращает ответ от LLM с источниками и метаданными.
    """
    settings = get_settings()
    logger.info(f"Получен запрос чата, session_id={request.session_id}")

    try:
        # Преобразуем сообщения в правильный формат
        formatted_messages = []
        for msg in request.messages:
            if isinstance(msg, dict):
                formatted_messages.append(
                    ChatMessage(
                        role=msg.get("role", "user"),
                        content=msg.get("content", "")
                    )
                )
            else:
                formatted_messages.append(msg)

        # Извлекаем последнее сообщение пользователя
        user_message = formatted_messages[-1].content if formatted_messages else ""

        # История сообщений без последнего. Для авторизованных — источник
        # истины СЕРВЕР (БД), а не тело запроса: клиент мог прислать устаревшую
        # localStorage-историю (или историю из другой вкладки), и она разъехалась
        # бы с БД. Берём из chat_storage, если сессия уже существует.
        history = [
            {"role": msg.role, "content": msg.content}
            for msg in formatted_messages[:-1]
        ] if formatted_messages else []
        if current_user and request.session_id:
            try:
                from src.api.services.chat_storage import chat_storage
                stored = chat_storage.list_messages(current_user.id, request.session_id)
                if stored:
                    history = [
                        {"role": m["role"], "content": m["content"]}
                        for m in stored
                    ]
            except Exception as e:
                logger.warning(f"Не удалось прочитать историю из БД: {e}")

        # Extract group_ids and admin status for document access control
        group_ids = [g.id for g in current_user.groups] if current_user and current_user.groups else None
        is_admin = current_user.is_admin if current_user else False

        # Генерируем ответ через chat_service (теперь через Provider Architecture)
        response = await chat_service.generate_response(
            user_message=user_message,
            session_id=request.session_id,
            history=history,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            use_rag=True,
            group_ids=group_ids,
            is_admin=is_admin
        )

        # Сохраняем сообщения на сервере (если пользователь авторизован).
        # Зачем: серверное хранение диалогов вместо localStorage — нет гонки
        # вкладок, история доступна с любого устройства и не теряется.
        if current_user:
            try:
                from src.api.services.chat_storage import chat_storage
                sid = response.get("session_id") or request.session_id or "session_" + str(uuid.uuid4())
                # Если клиент не прислал session_id — создаём сессию
                if not request.session_id:
                    created = chat_storage.create_session(current_user.id, title=user_message[:60])
                    sid = created["id"]
                # Проверяем, что сессия принадлежит пользователю (или создаём)
                existing = chat_storage.get_session(current_user.id, sid)
                if not existing:
                    created = chat_storage.create_session(current_user.id, title=user_message[:60])
                    sid = created["id"]
                # Сохраняем сообщение пользователя (если ещё не сохранялось —
                # в потоковом режиме клиент шлёт всю историю каждый раз)
                msgs = chat_storage.list_messages(current_user.id, sid)
                if not any(m["role"] == "user" and m["content"] == user_message for m in msgs[-5:]):
                    chat_storage.add_message(sid, "user", user_message)
                chat_storage.add_message(sid, "assistant", response["response"], metadata=response["metadata"])
            except Exception as e:
                logger.warning(f"Не удалось сохранить сообщения чата: {e}")

        return ChatResponse(
            id=response["id"],
            session_id=response["session_id"],
            response=response["response"],
            sources=response["sources"],
            metadata={
                "model": response["model"],
                "backend": response["backend"],
                "usage": response["usage"],
                "rag_used": response["metadata"]["rag_used"],
                "sources_count": response["metadata"]["sources_count"],
                "total_docs": response["metadata"]["total_docs"],
                "graph_used": response["metadata"]["graph_used"],
                "intent": response["metadata"].get("intent", "semantic"),
            }
        )

    except Exception as e:
        logger.error(f"Ошибка генерации ответа: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stream", summary="Потоковый ответ чата")
async def stream_message(
    request: ChatRequest,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Потоковая передача ответа от LLM (Server-Sent Events).

    Принимает те же параметры что и POST /, но возвращает SSE поток.
    """
    try:
        formatted_messages = []
        for msg in request.messages:
            if isinstance(msg, dict):
                formatted_messages.append(
                    ChatMessage(
                        role=msg.get("role", "user"),
                        content=msg.get("content", "")
                    )
                )
            else:
                formatted_messages.append(msg)

        user_message = formatted_messages[-1].content if formatted_messages else ""
        history = [
            {"role": msg.role, "content": msg.content}
            for msg in formatted_messages[:-1]
        ] if formatted_messages else []

        group_ids = [g.id for g in current_user.groups] if current_user and current_user.groups else None
        is_admin = current_user.is_admin if current_user else False

        async def event_stream():
            async for chunk in chat_service.generate_stream(
                user_message=user_message,
                session_id=request.session_id,
                history=history,
                group_ids=group_ids,
                is_admin=is_admin
            ):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )

    except Exception as e:
        logger.error(f"Ошибка потоковой генерации: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search", summary="Векторный поиск по чанкам")
async def search_chunks(
    request: dict,
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Векторный поиск по чанкам через Qdrant.
    Принимает {"query": "...", "limit": 10}
    """
    try:
        from src.indexing.embeddings_service import embeddings_service

        query = request.get("query", "")
        limit = request.get("limit", 10)

        if not query:
            return {"chunks": [], "total": 0}

        if embeddings_service._qdrant_client is None:
            await embeddings_service.initialize()

        chunks = await embeddings_service.search(query, limit=limit)
        return {"chunks": chunks, "total": len(chunks)}
    except Exception as e:
        logger.error(f"Search error: {e}")
        return {"chunks": [], "total": 0, "error": str(e)}


@router_export.post("/{session_id}", summary="Экспортировать диалог")
async def export_session(
    session_id: str,
    format: str = Query(default="docx", description="Формат: docx или pdf"),
    messages: Optional[list] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Экспортировать диалог сессии в документ.

    - **session_id**: ID сессии
    - **format**: Формат файла (docx или pdf)
    - **messages**: Список сообщений (фолбэк, если сессия не на сервере)

    Сообщения берутся с сервера (chat_storage), если пользователь
    авторизован; иначе — из тела запроса (обратная совместимость).
    """
    try:
        export_messages = messages or []
        if current_user:
            try:
                from src.api.services.chat_storage import chat_storage
                stored = chat_storage.list_messages(current_user.id, session_id)
                if stored:
                    export_messages = stored
            except Exception as e:
                logger.warning(f"Не удалось прочитать сессию из БД: {e}")

        if not export_messages:
            return Response(
                content="Сообщения не переданы и сессия не найдена на сервере",
                status_code=400,
                media_type="text/plain"
            )

        if format.lower() == "pdf":
            doc_bytes = export_service.export_to_pdf(
                messages=export_messages,
                title=f"Диалог KAG - {session_id[:8]}",
                author="KAG System"
            )
            media_type = "application/pdf"
            filename = f"kag_dialog_{session_id[:8]}.pdf"
        else:
            doc_bytes = export_service.export_to_docx(
                messages=export_messages,
                title=f"Диалог KAG - {session_id[:8]}",
                author="KAG System"
            )
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            filename = f"kag_dialog_{session_id[:8]}.docx"

        return Response(
            content=doc_bytes,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
        )

    except Exception as e:
        logger.error(f"Ошибка экспорта: {e}")
        raise HTTPException(status_code=500, detail=str(e))
