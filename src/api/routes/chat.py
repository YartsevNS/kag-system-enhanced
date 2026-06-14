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

        # История сообщений без последнего
        history = [
            {"role": msg.role, "content": msg.content}
            for msg in formatted_messages[:-1]
        ] if formatted_messages else []

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
                "graph_used": response["metadata"]["graph_used"]
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
    messages: Optional[list] = None
):
    """
    Экспортировать диалог сессии в документ.

    - **session_id**: ID сессии
    - **format**: Формат файла (docx или pdf)
    - **messages**: Список сообщений
    """
    try:
        if not messages:
            return Response(
                content="Сообщения не переданы",
                status_code=400,
                media_type="text/plain"
            )

        if format.lower() == "pdf":
            doc_bytes = export_service.export_to_pdf(
                messages=messages,
                title=f"Диалог KAG - {session_id[:8]}",
                author="KAG System"
            )
            media_type = "application/pdf"
            filename = f"kag_dialog_{session_id[:8]}.pdf"
        else:
            doc_bytes = export_service.export_to_docx(
                messages=messages,
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
