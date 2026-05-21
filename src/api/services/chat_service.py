"""
Сервис чата с интеграцией LLM и RAG

Объединяет:
- Поиск по векторной БД (RAG)
- Генерацию ответов через LLM
- Управление контекстом
- Историю сессий
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
import uuid
from loguru import logger

from src.llm import (
    LLMRequest,
    ChatMessage,
    MessageRole
)
from src.api.services.model_manager import model_manager
from src.indexing.embeddings_service import embeddings_service
from src.config import get_settings


class ChatService:
    """
    Сервис чата с RAG pipeline.

    Flow:
    1. Получить запрос пользователя
    2. Найти релевантные документы в Qdrant
    3. Сформировать промпт с контекстом
    4. Отправить в LLM
    5. Вернуть ответ с источниками
    """

    def __init__(self):
        """Инициализация сервиса"""
        settings = get_settings()
        self._max_context_length = settings.LLM_MAX_TOKENS
        self._temperature = settings.LLM_TEMPERATURE
        self._search_limit = 5  # Сколько документов искать

        logger.info("ChatService инициализирован")

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

        # Шаг 1: RAG поиск если включен
        if use_rag:
            try:
                logger.debug("Выполняю RAG поиск...")
                # 1a. Векторный поиск в Qdrant
                from src.indexing.embeddings_service import embeddings_service
                search_results = await embeddings_service.search(
                    query=user_message,
                    limit=self._search_limit,
                    group_ids=group_ids,
                    is_admin=is_admin
                )

                if search_results:
                    context_parts = []
                    for i, result in enumerate(search_results, 1):
                        doc_id = result.get('document_id', '?')[:12]
                        context_parts.append(
                            f"[Источник {i}] (doc:{doc_id}, score:{result['score']:.3f}):\n{result['content']}"
                        )
                    context = "\n\n".join(context_parts)
                    sources = search_results
                    logger.info(f"Qdrant: найдено {len(sources)} чанков")
                
                # 1b. Поиск в графе Neo4j — сущности, их типы и связи
                try:
                    from src.indexing.knowledge_graph import kg_service
                    # Разбиваем сообщение на слова-кандидаты (>=3 букв) + entities из Qdrant
                    import re
                    words = re.findall(r'[A-ZА-ЯЁ]{2,}|[A-Za-z]{3,}|[а-яё]{4,}', user_message)
                    # Добавляем ID документов из Qdrant для контекстного поиска
                    doc_ids_from_qdrant = list(set(
                        r.get('document_id') for r in (search_results or []) if r.get('document_id')
                    ))[:5]
                    entities_for_search = list(set(words))[:5]  # До 5 ключевых слов
                    
                    graph_results = kg_service.hybrid_search(entities_for_search, doc_ids_from_qdrant) if entities_for_search else []
                    if not graph_results:
                        # Fallback: поиск всех сущностей из найденных Qdrant-документов
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

        # Шаг 2: Формируем промпт с контекстом
        messages = self._build_messages(
            user_message=user_message,
            context=context,
            history=history or []
        )

        # Шаг 3: Генерация ответа через LLM
        logger.debug("Отправляю запрос в LLM...")
        
        llm_router = model_manager.llm_router
        if not llm_router:
            raise RuntimeError("LLM Router не инициализирован. Проверьте настройки LLM_BACKEND.")
        
        llm_request = LLMRequest(
            messages=messages,
            temperature=temperature or self._temperature,
            max_tokens=max_tokens or self._max_context_length,
            stream=False
        )

        llm_response = await llm_router.generate(llm_request)

        # Шаг 4: Логируем запрос к LLM
        from src.security.audit import audit_logger, AuditEventType
        audit_logger.log_llm_request(
            user_id=session_id or "anonymous",
            model=llm_response.model,
            prompt_length=sum(len(m.content) for m in messages),
            response_length=len(llm_response.generated_text),
            duration_seconds=0.0  # TODO: добавить замер времени
        )

        # Шаг 5: Формируем ответ
        response = {
            "id": llm_response.id,
            "session_id": session_id or str(uuid.uuid4()),
            "response": llm_response.generated_text,
            "model": llm_response.model,
            "backend": llm_response.backend.value,
            "sources": sources,
            "usage": {
                "prompt_tokens": llm_response.usage.prompt_tokens if llm_response.usage else 0,
                "completion_tokens": llm_response.usage.completion_tokens if llm_response.usage else 0,
                "total_tokens": llm_response.usage.total_tokens if llm_response.usage else 0
            },
            "metadata": {
                "rag_used": use_rag and len(sources) > 0,
                "sources_count": len(sources),
                "context_length": len(context),
                "generated_at": datetime.utcnow().isoformat()
            }
        }

        logger.info(
            f"Ответ сгенерирован: tokens={response['usage']['total_tokens']}, "
            f"sources={len(sources)}"
        )

        return response

    def _build_messages(
        self,
        user_message: str,
        context: str,
        history: List[Dict[str, str]]
    ) -> List[ChatMessage]:
        """
        Построить список сообщений для LLM.

        Args:
            user_message: Текущее сообщение пользователя
            context: Контекст из RAG поиска
            history: История сообщений

        Returns:
            Список ChatMessage
        """
        messages = []

        # Системное сообщение
        system_prompt = self._get_system_prompt(context)
        if system_prompt:
            messages.append(
                ChatMessage(
                    role=MessageRole.SYSTEM,
                    content=system_prompt
                )
            )

        # История сообщений
        for msg in history:
            role_str = msg.get("role", "user")
            try:
                role = MessageRole(role_str)
            except ValueError:
                # Если роль невалидна, используем user по умолчанию
                logger.warning(f"Неизвестная роль '{role_str}', использую 'user'")
                role = MessageRole.USER
            content = msg.get("content", "")
            messages.append(
                ChatMessage(role=role, content=content)
            )

        # Текущее сообщение пользователя
        messages.append(
            ChatMessage(
                role=MessageRole.USER,
                content=user_message
            )
        )

        return messages

    def _get_system_prompt(self, context: str) -> str:
        """
        Получить системный промпт из настроек или по умолчанию.

        Args:
            context: Контекст из RAG

        Returns:
            Системный промпт
        """
        # Пробуем загрузить из настроек
        default_prompt = (
            "Ты — AI-ассистент с доступом к гибридной базе знаний KAG.\n"
            "Ты работаешь с ДВУМЯ источниками данных: Qdrant (векторы — поиск по смыслу) и Neo4j (граф — сущности и связи).\n"
            "Начинай с анализа контекста из обеих баз. Если граф показывает связи — укажи это явно.\n"
            "Не выдумывай факты. Указывай источники. Структурируй ответ."
        )
        
        try:
            from src.api.services.config_store import config_store
            llm_config = config_store.get("llm", "default", {})
            base_prompt = llm_config.get("system_prompt", default_prompt)
        except:
            base_prompt = default_prompt

        if context:
            return f"""{base_prompt}

КОНТЕКСТ ИЗ ДОКУМЕНТОВ:
{context}

Используй вышеуказанный контекст для ответа на вопрос пользователя. Если контекст не содержит нужной информации, ответь на основе своих знаний, но укажи это."""
        else:
            return base_prompt

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
            group_ids: Группы пользователя для фильтрации документов
            is_admin: Если True, поиск возвращает все документы (без фильтрации)

        Yields:
            Чанки ответа
        """
        logger.info(f"Потоковая генерация: session={session_id}")

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
        messages = self._build_messages(user_message, context, history or [])

        # Потоковый запрос к LLM
        llm_request = LLMRequest(
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_context_length,
            stream=True
        )

        async for chunk in model_manager.llm_router.generate_stream(llm_request):
            yield {
                "delta": chunk.delta,
                "finish_reason": chunk.finish_reason,
                "model": chunk.model,
                "backend": chunk.backend.value
            }


# Глобальный экземпляр
chat_service = ChatService()
