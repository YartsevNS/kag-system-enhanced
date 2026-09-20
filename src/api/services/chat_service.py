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
import asyncio
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

    def _domain_mode(self) -> str:
        """Как домен вопроса участвует в поиске: hard | safe | off (настройка chat/domain).

        hard — прежнее поведение: фильтр по домену как отсечение. Опасно: у части
        документов домен не определён (в payload пусто), и они исчезают из выдачи
        целиком — чат отвечает «информация не найдена» при наличии документа в корпусе
        (замер 18.09.2026: 1012 чанков из 4944 без домена, вопрос про 2-МР давал 0.00
        с фильтром против 0.90 без него).
        safe — фильтр по домену, но пустой домен в выдачу допускается.
        off — домен в поиске не участвует (определяется, но не фильтрует).

        По умолчанию hard: поведение меняется только по замеру (см. docs/handoff.md).
        """
        try:
            from src.api.services.config_store import config_store
            raw = config_store.get("chat", "domain")
        except Exception:
            return "hard"
        mode = str(raw or "hard").strip().lower()
        return mode if mode in ("hard", "safe", "off") else "hard"

    def _domain_kwargs(self, domain: Optional[str]) -> dict:
        """Аргументы поиска по домену согласно режиму (см. _domain_mode)."""
        mode = self._domain_mode()
        if mode == "off" or not domain:
            return {"domain": None}
        return {"domain": domain, "domain_include_empty": mode == "safe"}

    async def _search_with_widening(self, query: str, limit: int, *, group_ids=None,
                                    is_admin: bool = False, user_id=None,
                                    domain: Optional[str] = None) -> list:
        """Поиск с фильтром по домену и расширением, если фильтр обеднил выдачу.

        Зачем расширение. Домен вопроса определяет классификатор, и он может не совпасть
        с доменом документа, где лежит ответ (или документ помечен universal/пустым).
        Тогда жёсткий фильтр отдаёт пустоту, и чат честно отвечает «информация не найдена»,
        хотя документ в корпусе есть. Замер 19.09.2026: у вопросов с нулевым баллом
        ВСЕ топовые фрагменты имели пустой домен и отсекались фильтром, а без фильтра
        правильный документ стоял на первом месте со score 0.915.

        Порядок: строгий поиск → если он пуст или явно слабый, повтор без фильтра
        и дополнение выдачи (дубли по id отбрасываются, итог пересортировывается по score).
        """
        from src.indexing.embeddings_service import embeddings_service

        kwargs = self._domain_kwargs(domain if domain else None)
        strict = await embeddings_service.search(
            query=query, limit=limit, group_ids=group_ids,
            is_admin=is_admin, user_id=user_id, **kwargs)
        if kwargs.get("domain"):
            _best = max((float(c.get("score") or 0) for c in strict), default=0.0)
            if len(strict) < max(3, limit // 2) or _best < 0.5:
                wide = await embeddings_service.search(
                    query=query, limit=limit, group_ids=group_ids,
                    is_admin=is_admin, user_id=user_id)
                _seen = {c.get("id") for c in strict if c.get("id")}
                _added = [c for c in wide if c.get("id") not in _seen]
                if _added:
                    logger.info(
                        f"[rag] домен «{kwargs.get('domain')}» обеднил выдачу: "
                        f"было {len(strict)}, добавлено {len(_added)} без фильтра")
                strict = strict + _added
        strict.sort(key=lambda c: -float(c.get("score") or 0))
        return strict

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
        provider,
        extra_payload: Optional[dict] = None,
        timeout: float = 120.0,
    ) -> Dict[str, Any]:
        """
        Вызвать LLM через API провайдера (OpenAI-совместимый формат).

        Все провайдеры (Ollama, OpenAI, DeepSeek, OpenRouter)
        поддерживают /v1/chat/completions.
        extra_payload — дополнительные поля тела запроса (например
        chat_template_kwargs для llama.cpp: отключить think у MiniCPM5).
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
        if extra_payload:
            payload.update(extra_payload)

        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
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
                    body = resp.text
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

    # Домены, которые умеет распознавать query_analysis (по умолчанию).
    # Пользователь правит промпт с парами в админке — там могут быть и другие
    # домены; этот список — страховка для парсинга ответа модели.
    _QA_DOMAINS = [
        "legal", "medical", "technical", "infosec",
        "accounting", "financial", "universal", "general", "other",
    ]

    async def _comparison_context(self, query: str, group_ids=None, is_admin: bool = False,
                                  user_id=None):
        """Контекст для СРАВНИТЕЛЬНОГО вопроса: по документу на каждую сторону.

        Зачем: при обычном поиске фрагменты двух документов приходят вперемешку, и модель
        отвечает без привязки «что к какому документу относится». Здесь на каждую сторону
        сравнения берём карточку документа (level=document) и её фрагменты (фильтр по
        document_id) — получается структура «объект 1 / объект 2», по которой модель может
        дать сравнение по пунктам.

        Возвращает (текст_контекста, список_документов). Пусто, если вопрос не сравнительный
        или режим выключен настройкой chat/comparison.
        """
        try:
            from src.indexing.comparison import (
                build_comparison_context, comparison_enabled, comparison_entities,
                is_comparison_question,
            )
            if not comparison_enabled() or not is_comparison_question(query):
                return "", []
            from src.indexing.comparison import split_comparison_sides
            # Сначала дешёвое разбиение по союзу (без LLM): работает и на коротких вопросах,
            # где _decompose_query молчит из-за своего порога длины (живой случай: режим
            # не включался ни разу). LLM-декомпозиция — только как резерв.
            sides_q = split_comparison_sides(query)
            if not sides_q:
                subs = await self._decompose_query(query)
                sides_q = comparison_entities(query, subs)
            if not sides_q:
                return "", []
            from src.indexing.embeddings_service import embeddings_service
            sides = []
            for sub in sides_q:
                docs = await embeddings_service.search_documents(
                    sub, limit=1, user_id=user_id, group_ids=group_ids
                )
                if not docs:
                    continue
                card = docs[0]
                doc_id = card.get("document_id")
                chunks = []
                if doc_id:
                    chunks = await embeddings_service.search(
                        query=sub, limit=4, filters={"document_id": doc_id},
                        group_ids=group_ids, is_admin=is_admin, user_id=user_id,
                    )
                sides.append({"query": sub, "card": card, "chunks": chunks})
            if len(sides) < 2:
                return "", sides
            logger.info(
                f"[rag] сравнительный контекст: сторон {len(sides)}, "
                f"документы {[str(s['card'].get('document_id'))[:8] for s in sides]}"
            )
            return build_comparison_context(sides), [s["card"] for s in sides]
        except Exception as e:
            logger.debug(f"[rag] сравнительный контекст не собран: {e}")
            return "", []

    async def _decompose_query(self, query: str) -> List[str]:
        """Query Decomposition: разбить сложный вопрос на простые подвопросы.

        Использует модель function_map/query_analysis (мелкая, быстрая).
        Возвращает список подзапросов; если вопрос простой — [query].
        """
        # Простой/короткий вопрос не декомпозируем
        q = (query or "").strip()
        if len(q) < 60 or not any(m in q.lower() for m in
                                  [" и ", " а также", " также ", "сравн", "какие из", "перечисли", "отличи"]):
            return [q]
        try:
            cfg = provider_service.get_function_llm_config("query_analysis")
            if not cfg or not cfg.get("url") or not cfg.get("model"):
                return [q]
        except Exception:
            return [q]

        prompt = (
            "Разбей сложный вопрос на 2-4 простых подвопроса для поиска по базе документов. "
            "Верни ТОЛЬКО JSON, без пояснений: {\"subqueries\":[\"...\",\"...\"]}. "
            "Каждый подвопрос — самостоятельный, без «и»/«а также». Вопрос: " + q
        )
        messages = [{"role": "user", "content": prompt}]
        try:
            # Разбиение вопроса — короткий служебный вызов (на критическом пути ответа):
            # таймаут 30 с и резерв из привязки функции, чтобы зависший провайдер
            # не задерживал ответ пользователю на две минуты.
            try:
                dc_chain = provider_service.get_function_llm_chain("query_analysis") or [cfg]
            except Exception:
                dc_chain = [cfg]
            _last_err = None
            for _i, _c in enumerate(dc_chain):
                try:
                    result = await self._call_llm(
                        messages=messages,
                        model=_c.get("model", ""),
                        temperature=0.0,
                        max_tokens=200,
                        provider=type("QACfg", (), {
                            "url": _c.get("url", ""),
                            "api_key": _c.get("api_key", ""),
                            "type": _c.get("provider", "custom"),
                        })(),
                        timeout=30.0,
                    )
                    if (result.get("content") or "").strip():
                        if _i > 0:
                            logger.warning("Декомпозиция: ответ дан РЕЗЕРВНЫМ провайдером "
                                           f"{_c.get('provider')}/{_c.get('model')}")
                        break
                except Exception as _e:
                    _last_err = _e
                    result = {}
                if _i + 1 < len(dc_chain):
                    logger.warning("Декомпозиция: провайдер не ответил — пробую резерв")
            if _last_err and not (result.get("content") or "").strip():
                raise _last_err
            raw = (result.get("content") or "").strip()
            import re, json as _json
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                return [q]
            data = _json.loads(m.group(0))
            subs = [s.strip() for s in data.get("subqueries", []) if s and s.strip()]
            if subs:
                return subs[:4]
        except Exception as e:
            logger.warning(f"Query decomposition пропущен: {e}")

        # Эвристика для «сравни А и Б» / «А против Б» — если модель не разбила
        for sep in [" против ", " vs ", " VS ", " и "]:
            if sep in q and any(w in q.lower() for w in ["сравн", "отличи", "разниц"]):
                parts = [p.strip() for p in q.split(sep) if p.strip()]
                if len(parts) >= 2:
                    return parts[:3]
        return [q]

    async def _detect_query_analysis(self, query: str) -> Optional[dict]:
        """Определить домен вопроса через модель анализа запросов (function_map/query_analysis).

        Мелкая модель-классификатор (llama.cpp / Ollama) вызывается ДО основного
        LLM: возвращает домен (legal/medical/technical/infosec/...), по которому
        можно фильтровать RAG-поиск и выбирать словарь алиасов/схему графа.

        Промпт (system_prompt из админки) — few-shot пары «вопрос → домен»,
        которые пользователь правит для повышения точности. К нему АВТОМАТИЧЕСКИ
        добавляются пары «домен → сущности» из словаря алиасов (entity_aliases,
        approved): админ ведёт словарь — классификатор обучается без ручной
        синхронизации промпта.

        Returns:
            {"domain": str|None, "raw": str} или None (функция не настроена).
            None — не блокируем чат, если query_analysis не настроена.
        """
        try:
            cfg = provider_service.get_function_llm_config("query_analysis")
            if not cfg or not cfg.get("url") or not cfg.get("model"):
                return None
        except Exception as e:
            logger.warning(f"Query analysis конфиг недоступен: {e}")
            return None

        system_prompt = cfg.get("system_prompt") or (
            "Classify the user question domain. Answer with ONE word only: "
            "legal, medical, technical, infosec, accounting, universal."
        )
        # Автоматические пары из словаря алиасов (не перезаписываем промпт админа)
        auto_pairs = self._build_alias_domain_pairs()
        if auto_pairs:
            system_prompt = f"{system_prompt}\n\n{auto_pairs}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]

        params = cfg.get("parameters") or {}
        # Классификация домена — короткий вызов: длинный таймаут здесь вреден, потому что
        # он на критическом пути ответа пользователю (наблюдали 19.09.2026: провайдер завис,
        # и ответ пришёл через 122 с, хотя генерация заняла 2.3 с).
        qa_timeout = 30.0
        try:
            qa_chain = provider_service.get_function_llm_chain("query_analysis") or [cfg]
        except Exception:
            qa_chain = [cfg]
        result = {}
        for _i, _c in enumerate(qa_chain):
            extra_payload = None
            if _c.get("provider") == "llamacpp" and (_c.get("parameters") or {}).get("no_think", True):
                extra_payload = {"chat_template_kwargs": {"enable_thinking": False}}
            try:
                result = await self._call_llm(
                    messages=messages,
                    model=_c.get("model", ""),
                    temperature=(_c.get("parameters") or {}).get("temperature", params.get("temperature", 0.1)),
                    max_tokens=(_c.get("parameters") or {}).get("max_tokens", params.get("max_tokens", 150)),
                    provider=type(
                        "QACfg", (), {
                            "url": _c.get("url", ""),
                            "api_key": _c.get("api_key", ""),
                            "type": _c.get("provider", "custom"),
                        }
                    )(),
                    extra_payload=extra_payload,
                    timeout=qa_timeout,
                )
            except Exception as e:
                logger.warning(f"Query analysis вызов не удался: {e}")
                result = {}
            if (result.get("content") or "").strip():
                if _i > 0:
                    logger.warning(f"Query analysis: ответ дан РЕЗЕРВНЫМ провайдером "
                                   f"{_c.get('provider')}/{_c.get('model')}")
                break
            if _i + 1 < len(qa_chain):
                logger.warning(f"Query analysis: провайдер {_c.get('provider')} не ответил — пробую резерв")

        raw = (result.get("content") or "").strip()
        if not raw:
            return None

        domain = self._parse_domain(raw)
        logger.info(f"Query analysis: domain={domain}, ответ={raw[:120]!r}")
        return {"domain": domain, "raw": raw[:500]}

    def _build_alias_domain_pairs(self, limit: int = 80) -> str:
        """Собрать пары «домен → сущности» из словаря алиасов (entity_aliases).

        Approved-пары (reviewed=True, verdict=approved) с заполненным domain
        автоматически подставляются в промпт query_analysis — классификатор
        обучается на словаре, который админ ведёт вручную. Пары группируются
        по домену, чтобы промпт оставался компактным.

        Returns:
            Строка вида "- infosec: ФСТЭК России, СЗИ, антивирус" или "".
        """
        try:
            from src.database.session import get_session_local
            from src.database.entity_alias_models import EntityAlias
            maker = get_session_local()
            s = maker()
            try:
                rows = (
                    s.query(EntityAlias.alias, EntityAlias.domain)
                    .filter(
                        EntityAlias.reviewed.is_(True),
                        EntityAlias.verdict == "approved",
                        EntityAlias.domain.isnot(None),
                        EntityAlias.domain != "",
                    )
                    .limit(limit)
                    .all()
                )
            finally:
                s.close()
        except Exception as e:
            logger.debug(f"Алиасы для промпта query_analysis недоступны: {e}")
            return ""

        if not rows:
            return ""

        by_domain: Dict[str, list] = {}
        for alias, domain in rows:
            by_domain.setdefault(domain, []).append(alias)

        lines = ["Известные сущности по доменам (из словаря алиасов):"]
        for domain in sorted(by_domain):
            aliases = by_domain[domain][:15]
            lines.append(f"- {domain}: {', '.join(aliases)}")
        return "\n".join(lines)

    @staticmethod
    def _parse_domain(raw: str) -> Optional[str]:
        """Извлечь домен из ответа модели.

        Приоритет: текст ПОСЛЕ последнего </think> (модель может рассуждать
        вслух, а итог писать в конце). Ищем последнее вхождение известного
        домена как целого слова.
        """
        import re
        text = raw
        # Если модель думала вслух — берём только финальную часть после </think>
        if "</think>" in text:
            parts = text.split("</think>")
            text = parts[-1]
            # Если после </think> пусто (модель не закончила) — берём и рассуждение
            if not text.strip() and len(parts) > 1:
                text = parts[-2]

        pattern = re.compile(
            r"\b(" + "|".join(ChatService._QA_DOMAINS) + r")\b", re.IGNORECASE
        )
        found = pattern.findall(text)
        if not found:
            # Fallback: ищем по всему ответу
            found = pattern.findall(raw)
        if not found:
            return None
        return found[-1].lower()

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

    @staticmethod
    def _access_guard(results: list, user_id: Optional[str], group_ids: Optional[list], is_admin: bool) -> list:
        """Post-guard: отфильтровать результаты по правам (2-й слой защиты).

        Дублирует ACL pre-filter из Qdrant — на случай регрессий фильтра,
        чтобы запрещённый чанк не попал в контекст LLM.
        """
        if is_admin or not results:
            return results
        gset = set(group_ids or [])
        out = []
        for r in results:
            v = r.get("visibility", "public")
            if v == "public":
                allowed = True
            else:
                allowed = bool(
                    (user_id and user_id in (r.get("allow_user_ids") or []))
                    or (gset and bool(gset & set(r.get("allow_group_ids") or [])))
                )
            if not allowed:
                continue
            if user_id and user_id in (r.get("deny_user_ids") or []):
                continue
            if gset and gset & set(r.get("deny_group_ids") or []):
                continue
            out.append(r)
        return out

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
        context_limit: Optional[int] = None,
        provider_id: Optional[str] = None,
        model: Optional[str] = None,
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
        # ── Настройки генерации: приоритет «запрос → привязка функции → встроенный дефолт» ──
        # Зачем: до 19.09.2026 temperature/max_tokens из привязки функции (админка) на чат не
        # влияли — брались из запроса, а UI чата присылал свои 0.7/2048, и настройка в админке
        # вводила в заблуждение. Теперь админка задаёт значения по умолчанию, а явные значения
        # в запросе (скрипты, внешние клиенты) по-прежнему выигрывают.
        _fm_params = getattr(func_map, "parameters", None) or {}
        try:
            _cfg_temperature = float(_fm_params.get("temperature", 0.7))
        except (TypeError, ValueError):
            _cfg_temperature = 0.7
        try:
            _cfg_max_tokens = int(_fm_params.get("max_tokens", 4096))
        except (TypeError, ValueError):
            _cfg_max_tokens = 4096
        temp = temperature if temperature is not None else _cfg_temperature
        tokens = max_tokens if max_tokens is not None else _cfg_max_tokens

        # ── Работа с контекстом (тоже настраивается в привязке функции chat) ──
        # context_limit — сколько фрагментов уходит в промпт. Меньше фрагментов = короче промпт
        # (быстрее и дешевле), но выше риск потерять факт.
        # ЗАМЕР 20.09.2026 (внешний набор, 75 вопросов, по 4 прогона на настройку): глубина 10 —
        # 0.737, глубина 20 — 0.820 при +0,3 с на ответ, диапазоны не пересекаются. Значение 20
        # НЕ ставим по решению владельца: рост контекста дороже по токенам у платного провайдера,
        # глубины 20/30/40 проверяем позже на локальных моделях. Задача — docs/handoff.md,
        # раздел «Что дальше», п. 7; замер — раздел «Пилот второй волны».
        try:
            ctx_limit = int(_fm_params.get("context_limit", 10) or 10)
        except (TypeError, ValueError):
            ctx_limit = 10
        ctx_limit = max(3, min(20, ctx_limit))
        # Клиент может переопределить глубину на конкретный запрос (3..20, границы зажимаем
        # на сервере, а не только в UI). Приоритет: запрос → привязка функции → дефолт 10.
        _ctx_source = "привязка функции"
        if context_limit is not None:
            try:
                ctx_limit = max(3, min(20, int(context_limit)))
                _ctx_source = "запрос"
            except (TypeError, ValueError):
                pass
        # mark_best — пометить первый (лучший по score) фрагмент: модель опирается на него раньше.
        mark_best = bool(_fm_params.get("mark_best", False))
        # min_top_score — честный отказ без вызова LLM, если лучший фрагмент слишком далёк
        # (0 = выключено). Экономит время на вопросах вне корпуса.
        try:
            min_top_score = float(_fm_params.get("min_top_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            min_top_score = 0.0

        # Отключение размышлений у reasoning-моделей делается в цикле ниже под каждого
        # провайдера цепочки (_extra_for): у основного и резервного типы могут отличаться.
        # Замер на стенде 19.09.2026: без этого параметра модель уходила в reasoning до
        # всего лимита и возвращала пустой content (при 200–700 токенах ответ был 0 символов).
        # Работает у deepseek/openai/openrouter/совместимых (thinking.type=disabled).

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
        # Домен вопроса через мелкую модель-классификатор (function_map/query_analysis).
        # Вызывается ДО RAG: в будущем по domain можно фильтровать поиск
        # (document_type в Qdrant), выбирать словарь алиасов и схему графа.
        # Если query_analysis не настроена в админке — быстро возвращает None,
        # чат работает как раньше.
        domain = None
        query_analysis_raw = None
        if use_rag and intent is None:
            try:
                qa = await self._detect_query_analysis(user_message)
                if qa:
                    domain = qa.get("domain")
                    query_analysis_raw = qa.get("raw", "")[:200]
            except Exception as e:
                logger.warning(f"Query analysis пропущен: {e}")
        meta_context = ""
        if intent == "count":
            # Для «сколько» хватает stats_line («В базе знаний загружено
            # документов: N») — не раздуваем промпт списком.
            meta_context = ""
            logger.info("Мета-запрос (count): RAG пропущен, отвечу по stats_line")
        elif intent == "list":
            # Хелпер синхронный и ходит в БД: в async-методе это блокирует
            # event loop на время запроса (а мы это делаем на КАЖДЫЙ запрос
            # пользователя к чату) — уводим в поток.
            meta_context = await asyncio.to_thread(
                self._build_documents_list_context,
                user_message, group_ids, is_admin, 25,
            )
            logger.info(
                "Мета-запрос (list): RAG пропущен, использую список из БД (25)"
            )

        # Сравнительный вопрос: заранее собираем структурированный контекст по сторонам.
        # Дешевле сделать это до обычного поиска: если вопрос не сравнительный, метод
        # вернёт пусто без лишних вызовов LLM.
        _comparison_block = ""
        try:
            _comparison_block, _cmp_docs = await self._comparison_context(
                user_message, group_ids=group_ids, is_admin=is_admin, user_id=user_id
            )
        except Exception as _e:
            logger.debug(f"[rag] сравнительный режим не сработал: {_e}")

        # Шаг 2: RAG поиск если включен
        if use_rag and intent is None:
            try:
                logger.debug("Выполняю RAG поиск...")
                # Логируем эффективную глубину: по этим строкам собирается статистика для
                # подбора дефолта (какое k реально используется и откуда оно взялось).
                logger.info(f"[rag] глубина контекста: {ctx_limit} фрагментов (источник: {_ctx_source})")
                from src.indexing.embeddings_service import embeddings_service
                # Поиск релевантных чанков
                search_results = await self._search_with_widening(
                    user_message, ctx_limit,
                    group_ids=group_ids, is_admin=is_admin, user_id=user_id,
                    domain=domain,
                )

                # Порог «нет ответа»: если лучший фрагмент слишком далёк по score, честно
                # отказываем БЕЗ вызова LLM (быстрее и дешевле). Выключено при 0.
                # Осторожно: контролировать порогом «ответа нет» ненадёжно — вопрос, которого
                # в корпусе нет, может дать высокий score (замер 18.09.2026), поэтому значение
                # подбирается по замеру и по умолчанию выключено.
                if min_top_score > 0 and search_results:
                    _top = max((float(c.get("score") or 0) for c in search_results), default=0.0)
                    if _top < min_top_score:
                        logger.info(f"[rag] отказ по порогу: лучший фрагмент {_top:.3f} < {min_top_score:.2f}")
                        return {
                            "id": str(uuid.uuid4()),
                            "session_id": session_id or str(uuid.uuid4()),
                            "response": ("В загруженных документах эта информация не найдена: "
                                         "ближайшие найденные фрагменты слишком далеки от вопроса."),
                            "model": model_name,
                            "backend": provider.type,
                            "sources": [],
                            "usage": {},
                            "metadata": {"rag_used": True,
                                         "sources_count": 0,
                                         "refused_low_score": True,
                                         "top_score": round(_top, 4),
                                         "total_docs": self._get_total_docs(),
                                         "generated_at": datetime.utcnow().isoformat()},
                        }

                if search_results:
                    # Приоритет содержательным фрагментам: титульные листы, оглавления и
                    # колонтитулы нужны в индексе (по ним ищут номер документа), но в контексте
                    # ответа занимают место, которое должно достаться содержанию (замер
                    # 2026-09-13: система честно отвечала «в контексте только титул и
                    # содержание Р 50.1.112-2016»). Служебные не выбрасываем — ставим в конец,
                    # чтобы при нехватке содержательных они всё равно попали в ответ.
                    try:
                        from src.indexing.service_chunks import order_context
                        search_results, _moved = order_context(search_results, min_substantive=3)
                        if _moved:
                            logger.debug(f"[rag] служебных фрагментов в конец контекста: {_moved}")
                    except Exception as e:
                        logger.debug(f"[rag] порядок контекста не перестроен: {e}")
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
                        from src.indexing.ids import display_filename
                        result['filename'] = display_filename(filename) or doc_id[:12]
                        score_info = f"rerank:{result.get('rerank_score', 0):.3f}" if 'rerank_score' in result else f"score:{result['score']:.3f}"
                        # Пометка лучшего фрагмента (настройка mark_best): модель видит, на что
                        # опираться в первую очередь, и реже «размазывает» ответ по всему контексту.
                        _mark = (" — САМЫЙ РЕЛЕВАНТНЫЙ ФРАГМЕНТ (отвечай по нему в первую очередь)"
                                 if (mark_best and i == 1) else "")
                        context_parts.append(
                            f"[Источник {i}{_mark}] «{filename or doc_id[:12]}» ({score_info}):\n{result['content']}"
                        )
                    context = "\n\n".join(context_parts)
                    if _comparison_block:
                        # Структура «объект 1 / объект 2» идёт ПЕРВОЙ, дальше обычные
                        # фрагменты — так модель видит привязку к документам до деталей.
                        context = _comparison_block + "\n\n" + context
                    sources = search_results

                    # ── Query Decomposition: сложный вопрос → подзапросы ────
                    # Для «сравни А и Б», «какие из X и Y» и т.п. разбиваем на
                    # подвопросы и дополняем поиск (уникальные чанки).
                    try:
                        _subs = await self._decompose_query(user_message)
                        if len(_subs) > 1:
                            _seen_ids = set(r.get("id") for r in search_results if r.get("id"))
                            _added = 0
                            for _sq in _subs[:4]:
                                _extra = await embeddings_service.search(
                                    query=_sq, limit=5, group_ids=group_ids, is_admin=is_admin,
                                    user_id=user_id,
                                    **self._domain_kwargs(domain if domain else None),
                                )
                                for _r in _extra:
                                    _rid = _r.get("id")
                                    if _rid and _rid not in _seen_ids:
                                        _seen_ids.add(_rid)
                                        search_results.append(_r)
                                        _added += 1
                            if _added:
                                logger.info(f"Query decomposition: {len(_subs)} подзапроса, добавлено чанков: {_added}")
                    except Exception as e:
                        logger.debug(f"Query decomposition пропущен: {e}")

                    # ── Post-guard: 2-й слой проверки прав доступа ──────────
                    search_results = self._access_guard(search_results, user_id, group_ids, is_admin)

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

                # 2c. Вычисления по таблицам: цифры считает SQL, а не модель по тексту.
                # Зачем: «сколько позиций и на какую сумму» — вопрос к данным, а не к
                # похожим фрагментам. Если сумма посчитана по найденным кускам, она зависит
                # от того, что попало в контекст, и молча получается заниженной.
                try:
                    from src.indexing.tables_settings import get_tables_config

                    if get_tables_config().get("sql_enabled", True):
                        from src.indexing.table_router import answer_from_tables, context_block

                        _table_docs = list({
                            r.get('document_id') for r in (search_results or [])
                            if r.get('document_id')
                        })[:5]
                        # Синхронные запросы к БД — в отдельный поток, иначе api заблокируется
                        t_res = await asyncio.to_thread(
                            answer_from_tables, user_message, _table_docs)
                        block = context_block(t_res)
                        if block:
                            context += block
                            logger.info(
                                f"[tables] SQL по таблицам: статус {t_res.get('status')}, "
                                f"строк {t_res.get('row_count')}, источник {t_res.get('source')}"
                            )
                except Exception as e:
                    logger.debug(f"табличный слой пропущен: {e}")

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

        # Инструкция к сравнительному ответу — только когда контекст сравнения собран
        if _comparison_block:
            from src.indexing.comparison import COMPARISON_INSTRUCTION
            system_prompt = f"{system_prompt}\n\n{COMPARISON_INSTRUCTION}"

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

        # Шаг 4: Вызов LLM. Цепочка «основной → резервный»: у одного провайдера могут быть
        # несколько потребителей на один ключ (чат, обработка документов, тесты) — при сбое
        # или лимитах запрос висит до таймаута. Если основной не ответил (пустой content,
        # ошибка, таймаут), пробуем резервный из привязки функции (Админка → Модели LLM).
        chain = []
        try:
            # provider_id/model — выбор пользователя в чате (клик по названию модели):
            # если он выбрал другого провайдера, работаем с ним, а резерв берём из привязки.
            chain = provider_service.get_function_provider_chain(
                "chat", provider_id or "", model or "")
        except Exception as _e:
            logger.debug(f"цепочка провайдеров чата не получена: {_e}")
        if not chain:
            chain = [(provider, model or model_name)]

        def _extra_for(prov) -> Optional[dict]:
            """Доп. параметры под конкретного провайдера (отключение размышлений)."""
            try:
                if bool(_fm_params.get("no_think", True)) and prov.type in (
                        "deepseek", "openai", "openrouter", "custom"):
                    return {"thinking": {"type": "disabled"}}
            except Exception:
                pass
            return None

        llm_result = {}
        fallback_used = False
        for _idx, (_prov, _model) in enumerate(chain):
            _model = _model or model_name
            logger.debug(f"Запрос в LLM: provider={_prov.type}/{_prov.name}, model={_model}")
            llm_result = await self._call_llm(
                messages=api_messages,
                model=_model,
                temperature=temp,
                max_tokens=tokens,
                provider=_prov,
                extra_payload=_extra_for(_prov),
            )
            _content = (llm_result.get("content") or "").strip()
            _ok = bool(_content) and not llm_result.get("error") and not _content.startswith("❌")
            if _ok:
                if _idx > 0:
                    fallback_used = True
                    logger.warning(f"Ответ дан РЕЗЕРВНЫМ провайдером: {_prov.name} / {_model}")
                provider, model_name = _prov, _model
                break
            _next = chain[_idx + 1][0].name if _idx + 1 < len(chain) else None
            logger.warning(f"Провайдер {_prov.name} не ответил ({_content[:60]!r})"
                           + (f" — пробую резерв {_next}" if _next else " — резерва нет"))

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
                "domain": domain,
                "provider": provider.name,
                "fallback_used": fallback_used,
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
        is_admin: bool = False,
        user_id: Optional[str] = None,
        context_limit: Optional[int] = None,
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
        # Настройки контекста из привязки функции chat — те же, что в обычном пути
        # (generate_response), чтобы потоковый ответ не расходился с обычным.
        _fm_params = getattr(func_map, "parameters", None) or {}
        try:
            ctx_limit = max(3, min(20, int(_fm_params.get("context_limit", 10) or 10)))
        except (TypeError, ValueError):
            ctx_limit = 10
        if context_limit is not None:
            try:  # клиент может задать глубину на запрос; границы зажимаем на сервере
                ctx_limit = max(3, min(20, int(context_limit)))
            except (TypeError, ValueError):
                pass
        mark_best = bool(_fm_params.get("mark_best", False))

        # RAG поиск
        from src.indexing.embeddings_service import embeddings_service
        # Домен для фильтра (лёгкий вызов query_analysis, если настроена)
        _stream_domain = None
        try:
            _qa = await self._detect_query_analysis(user_message)
            if _qa and _qa.get("domain"):
                _stream_domain = _qa["domain"]
        except Exception:
            pass
        search_results = await embeddings_service.search(
            query=user_message,
            limit=ctx_limit,
            group_ids=group_ids,
            is_admin=is_admin,
            user_id=user_id,
            **self._domain_kwargs(_stream_domain),
        )
        search_results = self._access_guard(search_results, user_id, group_ids, is_admin)

        context = ""
        if search_results:
            # Служебные фрагменты (титул/оглавление/колонтитул) — в конец контекста,
            # как и в основном пути ответа (см. src/indexing/service_chunks.py).
            try:
                from src.indexing.service_chunks import order_context
                search_results, _moved = order_context(search_results, min_substantive=3)
                if _moved:
                    logger.debug(f"[rag/stream] служебных в конец контекста: {_moved}")
            except Exception as e:
                logger.debug(f"[rag/stream] порядок контекста не перестроен: {e}")
            context_parts = []
            for i, result in enumerate(search_results, 1):
                _mark = (" — САМЫЙ РЕЛЕВАНТНЫЙ ФРАГМЕНТ (отвечай по нему в первую очередь)"
                         if (mark_best and i == 1) else "")
                context_parts.append(
                    f"[Источник {i}{_mark}]: {result['content']}"
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
