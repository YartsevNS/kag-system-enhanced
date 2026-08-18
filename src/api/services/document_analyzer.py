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
        Анализирует первый чанк и возвращает метаданные.
        
        Args:
            document_id: ID документа
            first_chunk_text: Текст первого чанка
            filename: Исходное имя файла
            
        Returns:
            Dict с recognized_title, document_type, summary, topics
        """
        if not first_chunk_text or len(first_chunk_text.strip()) < 10:
            logger.debug(f"Слишком короткий чанк для анализа: {document_id}")
            return {}

        prompt = self._build_prompt(first_chunk_text, filename)
        
        # Настройки ТОЛЬКО из admin (function_map:doc_analysis)
        cfg = self._get_config()
        if not cfg or not cfg.get("model"):
            logger.warning(f"[analyze] Функция 'doc_analysis' не настроена в админке — анализ пропущен для {document_id}")
            return {}

        model = cfg.get("model")
        llm_url = cfg.get("url", "")
        api_key = cfg.get("api_key", "")
        provider = cfg.get("provider", "ollama")
        
        try:
            import aiohttp
            
            async with aiohttp.ClientSession() as session:
                if provider in ("openai", "deepseek", "openrouter"):
                    # OpenAI-совместимый API (chat/completions)
                    headers = {"Content-Type": "application/json"}
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "Ты — классификатор документов. Отвечай строго валидным JSON без markdown."},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 300,
                        "stream": False,
                    }
                    async with session.post(
                        f"{llm_url}/v1/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(f"LLM недоступен для анализа: {resp.status}")
                            return {}
                        data = await resp.json()
                        response = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                else:
                    # Ollama API (/api/generate)
                    async with session.post(
                        f"{llm_url}/api/generate",
                        json={
                            "model": model,
                            "prompt": prompt,
                            "stream": False,
                            "options": {"temperature": 0.1, "max_tokens": 300},
                        },
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(f"LLM недоступен для анализа: {resp.status}")
                            return {}
                        data = await resp.json()
                        response = data.get("response", "")
                
                # Парсим ответ (общий путь для всех провайдеров)
                result = self._parse_response(response, filename)
                if not result:
                    logger.warning(f"Анализ {document_id}: LLM вернул невалидный JSON, ответ: {response[:200]}")
                return result
                    
        except Exception as e:
            logger.warning(f"Ошибка анализа документа {document_id}: {type(e).__name__}: {e}")
            return {}

    def _build_prompt(self, text: str, filename: str) -> str:
        """Строит промпт для LLM."""
        # Берём первые ~2000 символов
        sample = text[:2000]
        
        return f"""Проанализируй начало документа и верни JSON с метаданными.

Имя файла: {filename}

Текст:
---
{sample}
---

Верни ТОЛЬКО валидный JSON (без markdown, без ```), строго такой формат:
{{"title": "краткое название документа", "type": "тип", "summary": "одно предложение о чём документ", "topics": ["тема1", "тема2"]}}

Тип выбери из: invoice, contract, report, letter, form, identity, medical, legal, financial, technical, other.
Если непонятно — поставь "other".
Пиши на русском."""

    def _parse_response(self, response: str, filename: str) -> Dict[str, Any]:
        """Парсит ответ LLM в структуру."""
        import json
        
        # Очищаем от markdown-кода
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3].strip()
        
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Пробуем найти JSON в тексте
            import re
            match = re.search(r'\{[^}]+\}', text)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return {}
            else:
                return {}
        
        # Валидируем типы
        valid_types = {"invoice", "contract", "report", "letter", "form", 
                       "identity", "medical", "legal", "financial", "technical", "other"}
        
        result = {}
        if "title" in data and data["title"]:
            result["recognized_title"] = str(data["title"])[:200]
        if "type" in data and data["type"] in valid_types:
            result["document_type"] = data["type"]
        if "summary" in data and data["summary"]:
            result["summary"] = str(data["summary"])[:500]
        if "topics" in data and isinstance(data["topics"], list):
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
            
            # Также обновляем payload в Qdrant (для поиска)
            try:
                from src.indexing.qdrant_service import get_qdrant_service
                qdrant = get_qdrant_service()
                
                # Обновляем все точки этого документа
                payload_update = {}
                if "document_type" in result:
                    payload_update["document_type"] = result["document_type"]
                if "summary" in result:
                    payload_update["summary"] = result["summary"]
                if "topics" in result:
                    payload_update["topics"] = result["topics"]
                
                if payload_update:
                    qdrant.set_payload(
                        filter={"must": [{"key": "document_id", "match": {"value": document_id}}]},
                        payload=payload_update
                    )
                    logger.debug(f"Qdrant payload обновлён для {document_id}")
            except Exception as e:
                logger.debug(f"Не удалось обновить Qdrant payload: {e}")
                
        except Exception as e:
            logger.error(f"Критическая ошибка анализа: {e}")


# Глобальный экземпляр
document_analyzer = DocumentAnalyzer()
