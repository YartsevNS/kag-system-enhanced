"""
Document Analyzer — фоновый анализ документов через LLM.

Анализирует первый чанк документа для извлечения:
- Названия (recognized_title)
- Типа документа (document_type) 
- Краткого описания (summary)
- Ключевых тем (topics)

Работает асинхронно в фоне, не блокирует загрузку.
Результаты сохраняются в config_store и Qdrant payload.
"""

from typing import Dict, Any, Optional
from loguru import logger

from src.api.services.config_store import config_store


class DocumentAnalyzer:
    """Анализирует документы через LLM и обогащает метаданные."""

    def __init__(self, llm_url: Optional[str] = None):
        # Модель и URL НЕ хардкодим — берутся из admin (function_map:doc_analysis
        # через provider_service). Fallback на phi4-mini убран: если функция не
        # настроена в админке, анализ документа просто пропускается.
        pass

    def _get_config(self):
        """Получить настройки LLM ТОЛЬКО из admin (function_map:doc_analysis)."""
        try:
            from src.api.services.provider_service import provider_service
            cfg = provider_service.get_function_llm_config("doc_analysis")
            if cfg and cfg.get("model"):
                return cfg
        except Exception:
            pass
        return None

    async def analyze_document(
        self,
        document_id: str,
        first_chunk_text: str,
        filename: str
    ) -> Dict[str, Any]:
        """
        Анализирует начало документа и возвращает метаданные (title/type/summary/topics).

        Две попытки: живой случай — модель иногда отвечает не-JSON-ом, а прежняя версия
        делала ОДНУ попытку и возвращала {} (следствие: у 0 из 51 документа был
        recognized_title, а карточка документа выходила пустой).
        """
        if not first_chunk_text or len(first_chunk_text.strip()) < 10:
            logger.debug(f"Слишком короткий чанк для анализа: {document_id}")
            return {}

        # Настройки ТОЛЬКО из admin (function_map:doc_analysis). Берём цепочку
        # «основной → резервный»: если основной провайдер не ответил, анализ повторяется
        # на резервном (привязка функции в админке).
        try:
            from src.api.services.provider_service import provider_service
            cfgs = provider_service.get_function_llm_chain("doc_analysis")
        except Exception as e:
            logger.debug(f"[analyze] цепочка провайдеров doc_analysis не получена: {e}")
            cfgs = []
        if not cfgs or not cfgs[0].get("model"):
            logger.warning(f"[analyze] Функция 'doc_analysis' не настроена в админке — анализ пропущен для {document_id}")
            return {}

        from src.indexing.document_kinds import vocabulary_line
        from src.indexing.document_topics import vocabulary_line as rubrics_line
        from src.indexing.document_facets import vocabulary_line as facets_line
        type_labels = vocabulary_line()
        rubric_labels = rubrics_line()
        facet_labels = facets_line()
        system_prompt = (cfgs[0].get("system_prompt") or "").replace("{type_labels}", type_labels)
        # Админский промпт может подставлять и списки словарей: без подстановки модель видит
        # плейсхолдер как текст и отвечает темами «из головы».
        system_prompt = system_prompt.replace("{rubric_labels}", rubric_labels)
        system_prompt = system_prompt.replace("{facet_labels}", facet_labels)
        if not system_prompt:
            system_prompt = "Ты — классификатор документов. Отвечай строго валидным JSON без markdown."
        prompt = self._build_prompt(first_chunk_text, filename, type_labels, rubric_labels, facet_labels)

        last_error = ""
        # (конфиг, попытка): основной дважды, затем резервный дважды — если резерв задан
        plan = [(cand, attempt) for cand in range(len(cfgs)) for attempt in (1, 2)]
        for cand, attempt in plan:
            cfg = cfgs[cand]
            model = cfg.get("model")
            llm_url = cfg.get("url", "")
            api_key = cfg.get("api_key", "")
            provider = cfg.get("provider", "ollama")
            if cand > 0 and attempt == 1:
                logger.warning(f"[analyze] {document_id}: основной провайдер не ответил — "
                               f"пробую РЕЗЕРВ {provider}/{model}")
            try:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    # «custom» — OpenAI-совместимые провайдеры, добавленные вручную (например
                    # polza.ai): их API тот же /v1/chat/completions, а не Ollama. Без этого
                    # анализ документа уходил в ветку Ollama и падал на разборе ответа.
                    if provider in ("openai", "deepseek", "openrouter", "gigachat", "custom"):
                        headers = {"Content-Type": "application/json"}
                        if api_key:
                            headers["Authorization"] = f"Bearer {api_key}"
                        messages = [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ]
                        if attempt > 1:
                            # Вторая попытка: тот же запрос, но с явным требованием формата
                            # и запасом по токенам (обрыв ответа = невалидный JSON).
                            messages.append({
                                "role": "user",
                                "content": "ВАЖНО: ответ — ТОЛЬКО валидный JSON без пояснений, "
                                           "markdown и текста вокруг.",
                            })
                        payload = {
                            "model": model,
                            "messages": messages,
                            "temperature": 0.1 if attempt == 1 else 0.0,
                            "max_tokens": 300 if attempt == 1 else 500,
                            "stream": False,
                        }
                        async with session.post(
                            f"{llm_url}/v1/chat/completions",
                            json=payload,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=120),
                        ) as resp:
                            if resp.status != 200:
                                last_error = f"HTTP {resp.status}"
                                logger.warning(f"LLM недоступен для анализа: {resp.status}")
                                continue
                            data = await resp.json()
                            response = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    else:
                        ollama_prompt = f"{system_prompt}{LF}{LF}{prompt}" if system_prompt else prompt
                        async with session.post(
                            f"{llm_url}/api/generate",
                            json={
                                "model": model,
                                "prompt": ollama_prompt,
                                "stream": False,
                                "options": {"temperature": 0.1, "max_tokens": 300},
                            },
                            timeout=aiohttp.ClientTimeout(total=120),
                        ) as resp:
                            if resp.status != 200:
                                last_error = f"HTTP {resp.status}"
                                logger.warning(f"LLM недоступен для анализа: {resp.status}")
                                continue
                            data = await resp.json()
                            response = data.get("response", "")
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"Ошибка анализа документа {document_id} (попытка {attempt}): {last_error}")
                continue

            result = self._parse_response(response, filename)
            if result:
                return result
            last_error = "невалидный JSON"
            logger.warning(
                f"Анализ {document_id}: невалидный JSON (попытка {attempt}), ответ: {response[:200]!r}"
            )

        logger.warning(f"Анализ {document_id}: результат не получен ({last_error})")
        return {}
    def _build_prompt(self, text: str, filename: str, type_labels: str = "",
                      rubric_labels: str = "", facet_labels: str = "") -> str:
        """Строит промпт для LLM."""
        # Берём первые ~2000 символов
        sample = text[:2000]
        if not type_labels:
            from src.indexing.document_kinds import vocabulary_line
            type_labels = vocabulary_line()
        if not rubric_labels:
            # Темы — из своего словаря (document_topics): список кодов в промпте и проверка ответа
            # по нему обязаны совпадать, иначе модель пишет тему, которой нет в фильтрах.
            from src.indexing.document_topics import vocabulary_line as rubrics_line
            rubric_labels = rubrics_line()
        if not facet_labels:
            # Фасеты — из своего словаря (document_facets): значения ЗАКРЫТЫЕ, ответ проверяется
            # по перечню, лишнее отбрасывается (как с типами связей в графе).
            from src.indexing.document_facets import vocabulary_line as facets_line
            facet_labels = facets_line()

        return f"""Проанализируй начало документа и верни JSON с метаданными.

Имя файла: {filename}

Текст:
---
{sample}
---

Верни ТОЛЬКО валидный JSON (без markdown, без ```), строго такой формат:
{{"title": "краткое название документа", "type": "тип", "rubrics": ["тема"], "facets": {{"protection_subject": ["data"], "normative_force": "mandatory"}}, "summary": "одно предложение о чём документ", "topics": ["тема1", "тема2"]}}

Тип выбери из: {type_labels}.
Если непонятно — поставь "other".

Темы (rubrics) выбери из: {rubric_labels}.
Тем может быть НЕСКОЛЬКО (это список) или ни одной — тогда пустой список.

Фасеты (facets) — только из ЗАКРЫТЫХ перечней, значение не из перечня писать нельзя: {facet_labels}.
У фасета protection_subject значений может быть несколько (список), у normative_force — одно.
Если фасет неприменим — не указывай его вовсе (пустой объект {{}} тоже допустим).
Пиши на русском."""

    @staticmethod
    def _extract_json(response: str) -> Optional[Dict[str, Any]]:
        """Достать JSON из ответа модели: markdown, пояснения, вложенные скобки.

        Старый разбор брал регексп \\{[^}]+\\} — он рвался на вложенных объектах и
        возвращал {} при обычной для моделей обёртке ```json.
        """
        import json
        import re

        if not response:
            return None
        text = response.strip()
        text = re.sub(r"```[a-zA-Z]*", "", text).replace("```", "").strip()

        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        if start >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[start:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
        return None

    def _parse_response(self, response: str, filename: str) -> Dict[str, Any]:
        """Парсит ответ LLM в структуру."""
        data = self._extract_json(response)
        if not isinstance(data, dict):
            return {}

        # Валидируем вид по ЕДИНОМУ словарю (document_kinds): код вне словаря не пишем —
        # иначе в базе появляется значение, которого не знают ни фильтры, ни подписи.
        from src.indexing.document_kinds import is_valid as is_valid_kind

        result = {}
        if data.get("title"):
            result["recognized_title"] = str(data["title"])[:200]
        if is_valid_kind(data.get("type")):
            result["document_type"] = data["type"]
        # Темы (рубрики) — по своему словарю: неизвестные коды отбрасываются, дубли убираются,
        # порядок берётся из словаря. Проверка общая для модели и для ручной правки (normalize).
        from src.indexing import document_topics as _topics

        rubrics = _topics.normalize(data.get("rubrics"))
        if rubrics:
            result["rubrics"] = rubrics
        # Фасеты — по ЗАКРЫТЫМ перечням своего словаря (document_facets): значение вне перечня
        # отбрасывается, многозначность у protection_subject сохраняется.
        from src.indexing import document_facets as _facets

        facets = _facets.normalize(data.get("facets"))
        if facets:
            result["facets"] = facets
        if data.get("summary"):
            result["summary"] = str(data["summary"])[:500]
        if isinstance(data.get("topics"), list):
            result["topics"] = [str(t)[:50] for t in data["topics"][:5]]

        return result

    async def analyze_and_save(self, document_id: str, first_chunk_text: str, filename: str):
        """Анализирует и сохраняет результат в SQL (DocumentRepository)."""
        try:
            result = await self.analyze_document(document_id, first_chunk_text, filename)
            
            if not result:
                return
            
            # Обновляем запись в SQL
            from src.api.services.document_repository import get_doc_repo
            doc_data = get_doc_repo().get_dict(document_id)
            if doc_data:
                if "recognized_title" in result:
                    doc_data["recognized_title"] = result["recognized_title"]
                if "document_type" in result:
                    doc_data["document_type"] = result["document_type"]
                if "summary" in result:
                    doc_data["summary"] = result["summary"]
                if "topics" in result:
                    doc_data["topics"] = result["topics"]
                if "rubrics" in result:
                    doc_data["rubrics"] = result["rubrics"]
                if "facets" in result:
                    doc_data["facets"] = result["facets"]
                
                get_doc_repo().upsert(document_id, doc_data)
                logger.info(f"Метаданные обновлены для {document_id}: {result.get('document_type', '?')} — {result.get('recognized_title', '?')}")
            
            # Также обновляем payload в Qdrant (для поиска и фильтров)
            # Раньше здесь звался qdrant.set_payload — такого метода у QdrantService
            # нет, ошибка тонула в debug, и document_type/summary/topics в payload
            # НИКОГДА не попадали. Обновляем через embeddings_service.
            try:
                from src.indexing.embeddings_service import embeddings_service, service_for_document
                payload_update = {}
                if "document_type" in result:
                    payload_update["document_type"] = result["document_type"]
                if "summary" in result:
                    payload_update["summary"] = result["summary"]
                if "topics" in result:
                    payload_update["topics"] = result["topics"]
                if "rubrics" in result:
                    payload_update["rubrics"] = result["rubrics"]
                if "facets" in result:
                    payload_update["facets"] = result["facets"]
                if payload_update:
                    _n = await service_for_document(document_id).update_document_payload(document_id, payload_update)
                    logger.info(f"Qdrant payload обновлён для {document_id}: точек {_n}")
            except Exception as e:
                logger.warning(f"Не удалось обновить Qdrant payload: {e}")
                
        except Exception as e:
            logger.error(f"Критическая ошибка анализа: {e}")


# Глобальный экземпляр
document_analyzer = DocumentAnalyzer()
