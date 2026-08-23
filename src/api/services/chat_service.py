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

    def _detect_meta_intent(self, query: str) -> Optional[str]:
        """Определить, является ли запрос «мета-запросом о базе» (не семантикой).

        Зачем: «покажи все документы» / «сколько документов» — это вопросы о
        КАТАЛОГЕ, а не о содержимом. RAG-поиск по Qdrant вернул бы топ-N
        релевантных чанков из пары документов, а не список. Это классическая
        задача маршрутизации интента: мета-запросы обрабатываются SQL-выборкой
        из БД, семантические — RAG.

        Возвращает тип мета-запроса или None:
          - "count" — сколько документов
          - "list"  — показать список документов
          - None    — семантический запрос (обычный RAG)
        """
        q = (query or "").lower().strip()

        # Считаем «сколько/количество» — отдельно от «список»
        count_words = ["сколько документ", "количество документ", "всего документ",
                       "сколько файл", "сколько загружен", "сколько в базе"]
        if any(w in q for w in count_words):
            return "count"

        # Список/перечень/реестр — просим показать документы целиком
        list_words = [
            "покажи все документ", "покажи документ", "все документ",
            "список документ", "перечисли документ", "перечень документ",
            "реестр документ", "какие документ", "какие есть документ",
            "список файл", "покажи файл", "все файл",
            "дай список", "покажи список", "выведи список",
        ]
        if any(w in q for w in list_words):
            return "list"

        return None

    def _build_documents_list_context(
        self, query: str, group_ids: Optional[List[str]], is_admin: bool,
        limit: int = 60
    ) -> str:
        """Собрать контекст «список документов» из БД (SQL), не из Qdrant.

        Возвращает строку вида:
          СПИСОК ДОКУМЕНТОВ (всего N):
          1. «filename» (id, дата, размер)
          ...

        Фильтрация по группам: не-админ видит только документы своих групп
        (как в RAG). Показываем не более `limit` имён — чтобы не переполнить
        контекст LLM; при большем количестве добавляем «и ещё N...».
        """
        try:
            from src.api.services.document_repository import get_doc_repo
            docs, total = get_doc_repo().list(limit=10000, status="completed")

            # Фильтр по группам (аналог RAG-фильтра в embeddings_service.search)
            if not is_admin and group_ids:
                gset = set(group_ids)
                docs = [d for d in docs if d.group_ids and gset.intersection(d.group_ids or [])]

            total = len(docs)
            if total == 0:
                return "СПИСОК ДОКУМЕНТОВ: в базе нет документов (или нет доступа к ним)."

            lines = []
            for i, d in enumerate(docs[:limit], 1):
                size_kb = (d.file_size or 0) / 1024
                created = d.created_at.strftime("%d.%m.%Y") if d.created_at else "?"
                lines.append(f"{i}. «{d.filename}» (id: {d.id[:8]}, {created}, {size_kb:.0f} КБ)")

            suffix = f"\n... и ещё {total - limit} документов" if total > limit else ""
            return f"СПИСОК ДОКУМЕНТОВ (всего {total}):\n" + "\n".join(lines) + suffix
        except Exception as e:
            logger.warning(f"Не удалось собрать список документов: {e}")
            return "СПИСОК ДОКУМЕНТОВ: ошибка получения списка."

    async def generate_response(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        use_rag: bool = True,
        group_ids: Optional[List[str]] = None,
        is_admin: bool = False,
        user_id: Optional[str] = None,
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
            user_id: Реальный пользователь (для audit-лога). Раньше в лог
                писался session_id — «user: test» был session_id, а не юзером.

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

        # ── Маршрутизация интента (мета-запросы о базе vs семантика) ──────
        # Зачем: «покажи все документы» — это вопрос о КАТАЛОГЕ. RAG вернул бы
        # топ-N похожих чанков, а не список. Определяем тип запроса ДО RAG:
        #   - "count": сколько документов (SQL count; полный список НЕ шлём —
        #     flash-модель возвращает пустой ответ на длинный промпт)
        #   - "list":  список документов (SQL select, первые 25 имён)
        #   - None:    семантический запрос — обычный RAG ниже.
        # Мета-запросы обрабатываются без Qdrant; LLM лишь форматирует ответ.
        # Ограничение 25: эмпирически deepseek-v4-flash на промпте >~2.5-3К
        # токенов (список 60 документов ≈ 3400) отвечает пустой строкой.
        intent = self._detect_meta_intent(user_message) if use_rag else None
        meta_context = ""
        if intent == "count":
            # Для «сколько» хватает stats_line («В базе знаний загружено
            # документов: N») — не раздуваем промпт списком.
            meta_context = ""
            logger.info("Мета-запрос (count): RAG пропущен, отвечу по stats_line")
        elif intent == "list":
            meta_context = self._build_documents_list_context(
                user_message, group_ids, is_admin, limit=25
            )
            logger.info(
                "Мета-запрос (list): RAG пропущен, использую список из БД (25)"
            )

        # Шаг 2: RAG поиск если включен
        if use_rag and intent is None:
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

                    # ── Обогащение источников таблицами (table RAG) ────────────
                    # Если чанк — таблица (markdown-структура) или документ имеет
                    # таблицы в document_tables — прикрепляем HTML-версии к source,
                    # чтобы чат мог отрендерить их пользователю структурно.
                    # Исследование (2026): лучший подход — слоёный: markdown для
                    # LLM-контекста + HTML для отображения пользователю
                    # (Microsoft Azure Document Intelligence v4.0, LlamaIndex).
                    try:
                        from src.database.session import get_session_local
                        from src.database.document_table_models import DocumentTable
                        _doc_tables_cache = {}
                        for src in sources:
                            did = src.get('document_id')
                            if not did or did in _doc_tables_cache:
                                continue
                            _maker = get_session_local()
                            _s = _maker()
                            try:
                                _tabs = _s.query(DocumentTable).filter_by(document_id=did).all()
                                _doc_tables_cache[did] = [t.to_dict() for t in _tabs[:3]]
                            finally:
                                _s.close()
                        for src in sources:
                            did = src.get('document_id')
                            tabs = _doc_tables_cache.get(did) or []
                            if tabs:
                                src['tables'] = tabs
                        _with_tables = sum(1 for s in sources if s.get('tables'))
                        if _with_tables:
                            logger.info(f"Table RAG: {_with_tables} источников с таблицами")
                    except Exception as e:
                        logger.debug(f"Table RAG обогащение пропущено: {e}")

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

        # Статистика базы (для системных вопросов «сколько документов» и т.п.)
        try:
            from src.api.services.document_repository import get_doc_repo
            _docs = get_doc_repo().get_all() or {}
            total_docs = len(_docs)
            stats_line = f"В базе знаний загружено документов: {total_docs}."
        except Exception:
            total_docs = None
            stats_line = ""

        # Системный промпт (из function_map, с контекстом RAG)
        if context or meta_context:
            # Для мета-запросов (список/сколько) контекст — это СПИСОК из БД,
            # для семантических — чанки из Qdrant. Никогда не оба сразу.
            rag_block = f"КОНТЕКСТ ИЗ ДОКУМЕНТОВ:\n{context}" if context else ""
            list_block = f"{meta_context}" if meta_context else ""
            api_messages.append({
                "role": "system",
                "content": f"""{system_prompt}

{stats_line}

{list_block}
{rag_block}

Отвечай СТРОГО на основе контекста выше. Если контекст не содержит ответа на вопрос — скажи честно «в загруженных документах эта информация не найдена». НЕ объясняй, как устроена система, если тебя не спросили об этом напрямую."""
            })
        else:
            api_messages.append({
                "role": "system",
                "content": f"{system_prompt}\n\n{stats_line}".strip()
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

        # Шаг 5: Логируем запрос (в audit — реальный пользователь, не session_id)
        from src.security.audit import audit_logger, AuditEventType
        audit_logger.log_llm_request(
            user_id=user_id or session_id or "anonymous",
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
                "intent": intent or ("semantic" if use_rag else "none"),
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

        # Статистика базы
        try:
            from src.api.services.document_repository import get_doc_repo
            _docs = get_doc_repo().get_all() or {}
            stats_line = f"В базе знаний загружено документов: {len(_docs)}."
        except Exception:
            stats_line = ""

        # Формируем сообщения
        if context:
            system_content = (
                f"{system_prompt}\n\n{stats_line}\n\nКОНТЕКСТ ИЗ ДОКУМЕНТОВ:\n{context}\n\n"
                "Отвечай СТРОГО на основе контекста выше. Если контекст не содержит ответа — "
                "скажи честно «в загруженных документах эта информация не найдена». "
                "НЕ объясняй, как устроена система, если тебя не спросили напрямую."
            )
        else:
            system_content = f"{system_prompt}\n\n{stats_line}".strip()
        api_messages = [{"role": "system", "content": system_content}]
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
