"""
Сервис чата с интеграцией LLM и RAG

Объединяет:
- Поиск по векторной БД (RAG)
- Генерацию ответов через LLM (через Provider Architecture)
- Управление контекстом
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
import uuid
import time
import httpx
from loguru import logger

from src.llm import (
    LLMRequest,
    ChatMessage as LLMChatMessage,
    MessageRole
)
from src.api.services.provider_service import provider_service


class ChatService:
    """
    Сервис чата с RAG pipeline.

    Flow:
    1. Получить запрос пользователя
    2. Получить провайдера и модель из function_map/chat (Provider Architecture)
    3. Найти релевантные документы в Qdrant
    4. Сформировать промпт с контекстом
    5. Отправить в LLM через API провайдера
    6. Вернуть ответ с источниками
    """

    def __init__(self):
        """Инициализация сервиса"""
        self._search_limit = 10  # Количество документов для контекста чата
        logger.info("ChatService инициализирован")

    def _get_chat_provider(self) -> tuple:
        """
        Получить провайдера и function_map для чата из Provider Architecture.

        Returns:
            (ProviderConfig, FunctionMap) или (None, None)
        """
        try:
            result = provider_service.get_function_provider("chat")
            if result:
                return result
        except Exception as e:
            logger.warning(f"Не удалось получить провайдера чата: {e}")

        # Fallback: пытаемся получить дефолтного провайдера
        try:
            providers = provider_service.list_providers()
            if providers:
                pid = providers[0]["id"]
                from src.api.services.provider_service import FunctionMap
                fm = FunctionMap(
                    function="chat",
                    provider_id=pid,
                    model="",
                )
                provider = provider_service.get_provider_with_key(pid)
                return (provider, fm) if provider else (None, None)
        except Exception as e:
            logger.warning(f"Fallback провайдера не сработал: {e}")

        return (None, None)

    async def _call_llm(
        self,
        messages: list,
        model: str,
        temperature: float,
        max_tokens: int,
        provider
    ) -> Dict[str, Any]:
        """
        Вызвать LLM через API провайдера (OpenAI-совместимый формат).

        Все провайдеры (Ollama, OpenAI, DeepSeek, OpenRouter)
        поддерживают /v1/chat/completions.
        """
        url = f"{provider.url.rstrip('/')}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                elapsed = time.time() - start

                if resp.status_code == 200:
                    data = resp.json()
                    choice = data.get("choices", [{}])[0]
                    content = choice.get("message", {}).get("content", "")
                    usage = data.get("usage", {})

                    return {
                        "id": data.get("id", str(uuid.uuid4())),
                        "content": content,
                        "model": data.get("model", model),
                        "usage": {
                            "prompt_tokens": usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0),
                        },
                        "elapsed": elapsed,
                        "provider": provider.type,
                    }
                else:
                    body = await resp.text()
                    logger.error(f"LLM API error {resp.status_code}: {body[:200]}")
                    return {
                        "id": str(uuid.uuid4()),
                        "content": f"❌ Ошибка LLM: HTTP {resp.status_code}",
                        "model": model,
                        "usage": {},
                        "elapsed": elapsed,
                        "provider": provider.type,
                        "error": body[:200],
                    }
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"LLM call failed: {e}")
            return {
                "id": str(uuid.uuid4()),
                "content": f"❌ Ошибка подключения к LLM: {e}",
                "model": model,
                "usage": {},
                "elapsed": elapsed,
                "provider": provider.type,
                "error": str(e),
            }

    async def generate_response(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        use_rag: bool = True,
        group_ids: Optional[List[str]] = None,
        is_admin: bool = False
    ) -> Dict[str, Any]:
        """
        Сгенерировать ответ с RAG.

        Args:
            user_message: Сообщение пользователя
            session_id: ID сессии
            history: История сообщений
            temperature: Температура генерации
            max_tokens: Максимум токенов
            use_rag: Использовать ли RAG поиск
            group_ids: Группы пользователя для фильтрации документов
            is_admin: Если True, поиск возвращает все документы (без фильтрации)

        Returns:
            Словарь с ответом и метаданными
        """
        logger.info(f"Генерация ответа: session={session_id}, use_rag={use_rag}")

        sources = []
        context = ""

        # Шаг 1: Получаем провайдера и function_map для чата
        provider, func_map = self._get_chat_provider()
        if not provider:
            return {
                "id": str(uuid.uuid4()),
                "session_id": session_id or str(uuid.uuid4()),
                "response": "❌ Не настроен провайдер для чата. Зайдите в Админку → Провайдеры LLM и добавьте провайдера, затем настройте привязку функций.",
                "model": "N/A",
                "backend": "none",
                "sources": [],
                "usage": {},
                "metadata": {
                    "rag_used": False,
                    "sources_count": 0,
                    "context_length": 0,
                    "generated_at": datetime.utcnow().isoformat()
                }
            }

        model_name = func_map.model if func_map and func_map.model else ""
        system_prompt = func_map.system_prompt if func_map and func_map.system_prompt else self._get_default_prompt()
        temp = temperature if temperature is not None else 0.7
        tokens = max_tokens or 4096

        # Шаг 2: RAG поиск если включен
        if use_rag:
            try:
                logger.debug("Выполняю RAG поиск...")
                from src.indexing.embeddings_service import embeddings_service
                # Поиск релевантных чанков
                search_results = await embeddings_service.search(
                    query=user_message,
                    limit=self._search_limit,  # Количество чанков для контекста
                    group_ids=group_ids,
                    is_admin=is_admin
                )

                if search_results:
                    # Формируем контекст из результатов поиска
                    context_parts = []
                    for i, result in enumerate(search_results, 1):
                        doc_id = result.get('document_id', '?')
                        filename = result.get('filename', '')
                        if not filename:
                            try:
                                from src.api.services.document_service import document_service
                                record = document_service.get_document(doc_id)
                                if record:
                                    filename = record.filename
                            except Exception:
                                pass
                        result['filename'] = filename or doc_id[:12]
                        score_info = f"rerank:{result.get('rerank_score', 0):.3f}" if 'rerank_score' in result else f"score:{result['score']:.3f}"
                        context_parts.append(
                            f"[Источник {i}] «{filename or doc_id[:12]}» ({score_info}):\n{result['content']}"
                        )
                    context = "\n\n".join(context_parts)
                    sources = search_results
                    logger.info(f"Qdrant + Rerank: найдено {len(sources)} чанков")

                # 2b. Поиск в графе Neo4j
                try:
                    from src.indexing.knowledge_graph import kg_service
                    import re
                    words = re.findall(r'[A-ZА-ЯЁ]{2,}|[A-Za-z]{3,}|[а-яё]{4,}', user_message)
                    doc_ids_from_qdrant = list(set(
                        r.get('document_id') for r in (search_results or []) if r.get('document_id')
                    ))[:5]
                    entities_for_search = list(set(words))[:5]

                    graph_results = kg_service.hybrid_search(entities_for_search, doc_ids_from_qdrant) if entities_for_search else []
                    if not graph_results:
                        graph_results = kg_service.hybrid_search([user_message], doc_ids_from_qdrant)

                    if graph_results:
                        graph_context = []
                        for r in graph_results[:5]:
                            fid = r.get('filename', '?')[:50]
                            gcnt = r.get('entity_count', 0)
                            graph_context.append(
                                f"[Граф] Документ: {fid} | Связанных сущностей: {gcnt}"
                            )
                        context += "\n\n--- ГРАФ ЗНАНИЙ (Neo4j) ---\n"
                        context += "\n".join(graph_context)
                        logger.info(f"Neo4j: найдено {len(graph_results)} связей в графе")
                except Exception as e:
                    logger.debug(f"Neo4j поиск пропущен: {e}")

            except Exception as e:
                logger.warning(f"RAG поиск не выполнен: {e}")
                sources = []
                context = ""

        # Шаг 3: Формируем сообщения для LLM
        api_messages = []

        # Системный промпт (из function_map, с контекстом RAG)
        if context:
            api_messages.append({
                "role": "system",
                "content": f"""{system_prompt}

КОНТЕКСТ ИЗ ДОКУМЕНТОВ:
{context}

Используй вышеуказанный контекст для ответа на вопрос пользователя. Если контекст не содержит нужной информации, ответь на основе своих знаний, но укажи это."""
            })
        else:
            api_messages.append({
                "role": "system",
                "content": system_prompt
            })

        # История сообщений
        for msg in (history or []):
            role = msg.get("role", "user")
            if role not in ("user", "assistant", "system"):
                role = "user"
            api_messages.append({
                "role": role,
                "content": msg.get("content", "")
            })

        # Текущее сообщение пользователя
        api_messages.append({
            "role": "user",
            "content": user_message
        })

        # Шаг 4: Вызов LLM через провайдера
        logger.debug(f"Отправляю запрос в LLM: provider={provider.type}, model={model_name}")
        llm_result = await self._call_llm(
            messages=api_messages,
            model=model_name,
            temperature=temp,
            max_tokens=tokens,
            provider=provider,
        )

        # Шаг 5: Логируем запрос
        from src.security.audit import audit_logger, AuditEventType
        audit_logger.log_llm_request(
            user_id=session_id or "anonymous",
            model=llm_result.get("model", model_name),
            prompt_length=sum(len(m.get("content", "")) for m in api_messages),
            response_length=len(llm_result.get("content", "")),
            duration_seconds=llm_result.get("elapsed", 0),
        )

        # Шаг 6: Формируем ответ
        response = {
            "id": llm_result.get("id", str(uuid.uuid4())),
            "session_id": session_id or str(uuid.uuid4()),
            "response": llm_result.get("content", ""),
            "model": llm_result.get("model", model_name),
            "backend": provider.type,
            "sources": sources,
            "usage": llm_result.get("usage", {}),
            "metadata": {
                "rag_used": use_rag and len(sources) > 0,
                "sources_count": len(sources),
                "context_length": len(context),
                "generated_at": datetime.utcnow().isoformat(),
                "total_docs": self._get_total_docs(),
                "graph_used": use_rag,
            }
        }

        logger.info(
            f"Ответ сгенерирован: model={response['model']}, "
            f"tokens={response['usage'].get('total_tokens', 0)}, "
            f"sources={len(sources)}, "
            f"elapsed={llm_result.get('elapsed', 0):.1f}s"
        )

        return response

    def _get_default_prompt(self) -> str:
        """Системный промпт по умолчанию."""
        return (
            "Ты — AI-ассистент с доступом к гибридной базе знаний KAG.\n"
            "Ты работаешь с ДВУМЯ источниками данных: Qdrant (векторы — поиск по смыслу) и Neo4j (граф — сущности и связи).\n"
            "Начинай с анализа контекста из обеих баз. Если граф показывает связи — укажи это явно.\n"
            "Не выдумывай факты. Указывай источники. Структурируй ответ."
        )

    def _get_total_docs(self) -> int:
        """Получить общее количество документов в системе."""
        try:
            from src.api.services.document_service import document_service
            return len(document_service._documents)
        except Exception:
            return 0

    async def generate_stream(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        group_ids: Optional[List[str]] = None,
        is_admin: bool = False
    ):
        """
        Потоковая генерация ответа.

        Args:
            user_message: Сообщение пользователя
            session_id: ID сессии
            history: История сообщений
            group_ids: Группы пользователя
            is_admin: Если True, поиск возвращает все документы

        Yields:
            Чанки ответа
        """
        logger.info(f"Потоковая генерация: session={session_id}")

        provider, func_map = self._get_chat_provider()
        if not provider:
            yield {"delta": "❌ Не настроен провайдер для чата.", "finish_reason": "stop", "model": "N/A", "backend": "none"}
            return

        model_name = func_map.model if func_map and func_map.model else ""
        system_prompt = func_map.system_prompt if func_map and func_map.system_prompt else self._get_default_prompt()

        # RAG поиск
        from src.indexing.embeddings_service import embeddings_service
        search_results = await embeddings_service.search(
            query=user_message,
            limit=self._search_limit,
            group_ids=group_ids,
            is_admin=is_admin
        )

        context = ""
        if search_results:
            context_parts = []
            for i, result in enumerate(search_results, 1):
                context_parts.append(
                    f"[Источник {i}]: {result['content']}"
                )
            context = "\n\n".join(context_parts)

        # Формируем сообщения
        api_messages = [{"role": "system", "content": system_prompt}]
        for msg in (history or []):
            role = msg.get("role", "user")
            if role in ("user", "assistant", "system"):
                api_messages.append({"role": role, "content": msg.get("content", "")})
        api_messages.append({"role": "user", "content": user_message})

        # Потоковый вызов LLM через провайдера
        url = f"{provider.url.rstrip('/')}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

        payload = {
            "model": model_name,
            "messages": api_messages,
            "temperature": 0.7,
            "max_tokens": 4096,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        yield {"delta": f"❌ HTTP {resp.status_code}", "finish_reason": "stop", "model": model_name, "backend": provider.type}
                        return
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                yield {"delta": "", "finish_reason": "stop", "model": model_name, "backend": provider.type}
                                return
                            try:
                                import json as _json
                                chunk = _json.loads(data_str)
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                finish = chunk.get("choices", [{}])[0].get("finish_reason")
                                if content or finish:
                                    yield {"delta": content, "finish_reason": finish, "model": model_name, "backend": provider.type}
                            except _json.JSONDecodeError:
                                pass
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield {"delta": f"❌ Ошибка: {e}", "finish_reason": "stop", "model": model_name, "backend": provider.type}


# Глобальный экземпляр
chat_service = ChatService()
