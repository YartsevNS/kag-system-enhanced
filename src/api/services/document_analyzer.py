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

        # Настройки ТОЛЬКО из admin (function_map:doc_analysis)
        cfg = self._get_config()
        if not cfg or not cfg.get("model"):
            logger.warning(f"[analyze] Функция 'doc_analysis' не настроена в админке — анализ пропущен для {document_id}")
            return {}

        model = cfg.get("model")
        llm_url = cfg.get("url", "")
        api_key = cfg.get("api_key", "")
        provider = cfg.get("provider", "ollama")

        from src.indexing.auto_tagger import DocumentType
        type_labels = ", ".join(t.value for t in DocumentType)
        system_prompt = (cfg.get("system_prompt") or "").replace("{type_labels}", type_labels)
        if not system_prompt:
            system_prompt = "Ты — классификатор документов. Отвечай строго валидным JSON без markdown."
        prompt = self._build_prompt(first_chunk_text, filename, type_labels)

        last_error = ""
        for attempt in (1, 2):
            try:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    if provider in ("openai", "deepseek", "openrouter", "gigachat"):
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
    def _build_prompt(self, text: str, filename: str, type_labels: str = "") -> str:
        """Строит промпт для LLM."""
        # Берём первые ~2000 символов
        sample = text[:2000]
        if not type_labels:
            from src.indexing.auto_tagger import DocumentType
            type_labels = ", ".join(t.value for t in DocumentType)

        return f"""Проанализируй начало документа и верни JSON с метаданными.

Имя файла: {filename}

Текст:
---
{sample}
---

Верни ТОЛЬКО валидный JSON (без markdown, без ```), строго такой формат:
{{"title": "краткое название документа", "type": "тип", "summary": "одно предложение о чём документ", "topics": ["тема1", "тема2"]}}

Тип выбери из: {type_labels}.
Если непонятно — поставь "other".
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

        # Валидируем типы — полный список из DocumentType (auto_tagger)
        from src.indexing.auto_tagger import DocumentType
        valid_types = {t.value for t in DocumentType}

        result = {}
        if data.get("title"):
            result["recognized_title"] = str(data["title"])[:200]
        if data.get("type") in valid_types:
            result["document_type"] = data["type"]
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
                
                get_doc_repo().upsert(document_id, doc_data)
                logger.info(f"Метаданные обновлены для {document_id}: {result.get('document_type', '?')} — {result.get('recognized_title', '?')}")
            
            # Также обновляем payload в Qdrant (для поиска и фильтров)
            # Раньше здесь звался qdrant.set_payload — такого метода у QdrantService
            # нет, ошибка тонула в debug, и document_type/summary/topics в payload
            # НИКОГДА не попадали. Обновляем через embeddings_service.
            try:
                from src.indexing.embeddings_service import embeddings_service
                payload_update = {}
                if "document_type" in result:
                    payload_update["document_type"] = result["document_type"]
                if "summary" in result:
                    payload_update["summary"] = result["summary"]
                if "topics" in result:
                    payload_update["topics"] = result["topics"]
                if payload_update:
                    _n = await embeddings_service.update_document_payload(document_id, payload_update)
                    logger.info(f"Qdrant payload обновлён для {document_id}: точек {_n}")
            except Exception as e:
                logger.warning(f"Не удалось обновить Qdrant payload: {e}")
                
        except Exception as e:
            logger.error(f"Критическая ошибка анализа: {e}")


# Глобальный экземпляр
document_analyzer = DocumentAnalyzer()
