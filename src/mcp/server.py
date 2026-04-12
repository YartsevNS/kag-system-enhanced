"""
MCP сервер для тестирования и интеграции

Реализация Model Context Protocol через FastAPI.
Поддерживает:
- JSON-RPC 2.0
- HTTP транспорт
- SSE (Server-Sent Events)
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
import json
import asyncio
from typing import AsyncGenerator

from src.mcp.tools import (
    search_knowledge,
    generate_response,
    evaluate_quality,
    get_system_status
)

app = FastAPI(
    title="KAG MCP Server",
    description="MCP сервер для тестирования и интеграции с KAG",
    version="0.1.0"
)

# Карта инструментов
TOOLS = {
    "search_knowledge": search_knowledge,
    "generate_response": generate_response,
    "evaluate_quality": evaluate_quality,
    "get_system_status": get_system_status
}


@app.post("/mcp/v1", summary="JSON-RPC endpoint")
async def mcp_endpoint(request: Request):
    """
    Основная точка входа для JSON-RPC запросов.
    
    Формат запроса:
    ```json
    {
        "jsonrpc": "2.0",
        "method": "search_knowledge",
        "params": {"query": "пример"},
        "id": 1
    }
    ```
    
    Формат ответа:
    ```json
    {
        "jsonrpc": "2.0",
        "result": {...},
        "id": 1
    }
    ```
    """
    try:
        body = await request.json()
        
        # Валидация JSON-RPC
        if body.get("jsonrpc") != "2.0":
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32600, "message": "Invalid Request"},
                    "id": None
                }
            )
        
        method = body.get("method")
        params = body.get("params", {})
        request_id = body.get("id")
        
        if method not in TOOLS:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": request_id
                }
            )
        
        # Вызов инструмента
        logger.info(f"MCP вызов: {method}")
        result = await TOOLS[method](params)
        
        return {
            "jsonrpc": "2.0",
            "result": result,
            "id": request_id
        }
        
    except Exception as e:
        logger.error(f"Ошибка MCP: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": str(e)},
                "id": body.get("id") if isinstance(body, dict) else None
            }
        )


@app.get("/mcp/v1/sse", summary="SSE endpoint для потоковой передачи")
async def sse_endpoint(request: Request):
    """
    Server-Sent Events для потоковой передачи ответов.
    
    Используется для стриминга генерации LLM.
    """
    
    async def event_generator() -> AsyncGenerator[str, None]:
        """Генератор событий SSE"""
        try:
            # TODO: Реализовать потоковую генерацию
            yield f"data: {json.dumps({'status': 'streaming_started'})}\n\n"
            yield f"data: {json.dumps({'status': 'complete'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/mcp/v1/tools", summary="Список доступных инструментов")
async def list_tools():
    """
    Получить список всех доступных инструментов MCP.
    """
    return {
        "tools": [
            {
                "name": "search_knowledge",
                "description": "Поиск знаний в векторной БД",
                "parameters": {
                    "query": "string (обязательно): Поисковый запрос",
                    "limit": "number (опционально): Количество результатов",
                    "filters": "object (опционально): Фильтры по метаданным"
                }
            },
            {
                "name": "generate_response",
                "description": "Генерация ответа через LLM",
                "parameters": {
                    "messages": "array (обязательно): История сообщений",
                    "stream": "boolean (опционально): Потоковая передача",
                    "temperature": "number (опционально): Температура"
                }
            },
            {
                "name": "evaluate_quality",
                "description": "Оценка качества генерации",
                "parameters": {
                    "response_id": "string (обязательно): Идентификатор ответа",
                    "metrics": "array (опционально): Метрики для оценки"
                }
            },
            {
                "name": "get_system_status",
                "description": "Получить статус системы",
                "parameters": {}
            }
        ]
    }


@app.get("/health", summary="Проверка работоспособности")
async def health():
    """Проверка работоспособности MCP сервера"""
    return {"status": "ok", "service": "kag-mcp"}
