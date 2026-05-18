"""
Entity Extractor — извлечение сущностей и фактов из чанков через LLM.

После чанкинга каждый чанк анализируется для извлечения:
- Сущностей (люди, организации, даты, суммы, места, термины)
- Связей между сущностями
- Фактов (ключевые утверждения)

Результаты сохраняются в Knowledge Graph (Neo4j) и config_store.
Работает асинхронно в фоне, не блокирует обработку.
"""

from typing import Dict, Any, List, Optional
from loguru import logger
import json
import re

from src.api.services.config_store import config_store


class EntityExtractor:
    """Извлекает сущности и факты из чанков через LLM."""

    def __init__(self):
        self._model = "phi4-mini"
        self._llm_url = "http://192.168.50.41:11434"

    def _get_config(self):
        try:
            from src.api.routes.admin_models import _ext_llm_config
            return _ext_llm_config
        except Exception:
            return None

    def _get_graph_config(self):
        """Get graph model config: first try config_store (persistent), then in-memory."""
        try:
            from src.api.services.config_store import config_store
            saved = config_store.get("graph_model", "default")
            if saved and saved.get("model"):
                return saved
        except Exception:
            pass
        try:
            from src.api.routes.admin_models import _graph_model_config
            return _graph_model_config
        except Exception:
            return None

    async def extract_from_chunk(
        self, 
        chunk_text: str, 
        chunk_id: str,
        document_id: str,
        filename: str = ""
    ) -> Dict[str, Any]:
        """Извлечь сущности из одного чанка."""
        if not chunk_text or len(chunk_text.strip()) < 20:
            return {"entities": [], "relations": [], "facts": []}

        graph_cfg = self._get_graph_config()
        if graph_cfg and graph_cfg.get('model'):
            model = graph_cfg['model']
            # Use URL from graph config, fallback to default Ollama
            llm_url = graph_cfg.get('url') or self._llm_url
        else:
            cfg = self._get_config()
            llm_url = cfg.url if cfg else self._llm_url
            model = cfg.model if cfg else self._model

        prompt = self._build_extraction_prompt(chunk_text, filename)

        try:
            import aiohttp
            # Build request payload based on provider
            provider = (graph_cfg or {}).get('provider', 'ollama')
            api_key = (graph_cfg or {}).get('api_key', '')
            
            if provider == 'ollama':
                req_url = f"{llm_url}/api/generate"
                req_json = {
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.05, "max_tokens": 500}
                }
                req_headers = {}
            else:
                # OpenAI-compatible API (OpenAI, DeepSeek, OpenRouter)
                req_url = f"{llm_url}/v1/chat/completions"
                req_json = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.05,
                    "max_tokens": 500
                }
                req_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    req_url,
                    json=req_json,
                    headers=req_headers,
                    timeout=aiohttp.ClientTimeout(total=180)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"LLM вернул {resp.status} для {chunk_id} (модель {model}, provider {provider})")
                        return {"entities": [], "relations": [], "facts": []}
                    data = await resp.json()
                    # Parse response based on provider
                    if provider == 'ollama':
                        response_text = data.get("response", "")
                    else:
                        response_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    result = self._parse_response(response_text)
                    if not result.get("entities") and not result.get("relations"):
                        logger.warning(f"Не удалось извлечь сущности из {chunk_id}: модель {model} ({provider}), ответ: {response_text[:200]}")
                    return result
        except Exception as e:
            logger.warning(f"Ошибка извлечения сущностей из {chunk_id}: {type(e).__name__}: {e}")
            return {"entities": [], "relations": [], "facts": []}

    def _build_extraction_prompt(self, text: str, filename: str) -> str:
        sample = text[:1500]
        return f"""Извлеки из текста сущности и факты. Верни ТОЛЬКО валидный JSON (без markdown).

Документ: {filename}

Текст:
---
{sample}
---

Формат ответа (строго JSON):
{{
  "entities": [
    {{"name": "имя или название", "type": "тип", "properties": {{"ключ": "значение"}}}}
  ],
  "relations": [
    {{"source": "сущность1", "target": "сущность2", "type": "тип связи"}}
  ],
  "facts": ["факт 1", "факт 2"]
}}

Типы сущностей (entity.type): person, organization, date, money, location, document_ref, legal_term, amount, phone, email
Типы связей (relation.type): SIGNED_BY, DATED, AMOUNT, BELONGS_TO, MENTIONS, RELATED_TO, WORKS_AT, LOCATED_IN

Пиши на русском. Не выдумывай — только то, что явно есть в тексте."""

    def _parse_response(self, response: str) -> Dict[str, Any]:
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return {"entities": [], "relations": [], "facts": []}

    async def extract_and_store(
        self,
        document_id: str,
        chunk_id: str,
        chunk_text: str,
        chunk_seq: int = 0,
        filename: str = ""
    ):
        """Извлечь и сохранить в граф знаний."""
        try:
            result = await self.extract_from_chunk(chunk_text, chunk_id, document_id, filename)

            if not result.get("entities") and not result.get("relations"):
                return

            from src.indexing.knowledge_graph import kg_service

            # Сохраняем сущности
            for ent in result.get("entities", []):
                if not ent.get("name"):
                    continue
                try:
                    from src.indexing.knowledge_graph import Entity
                    entity = Entity(
                        name=str(ent["name"])[:200],
                        type=str(ent.get("type", "unknown"))[:50],
                        chunk_id=chunk_id,
                        document_id=document_id,
                        confidence=0.8,
                        properties=ent.get("properties", {})
                    )
                    kg_service.create_entity(entity)
                except Exception as e:
                    logger.debug(f"Ошибка сохранения сущности {ent.get('name')}: {e}")

            # Сохраняем связи
            for rel in result.get("relations", []):
                if not rel.get("source") or not rel.get("target"):
                    continue
                try:
                    from src.indexing.knowledge_graph import Relation
                    relation = Relation(
                        source=str(rel["source"])[:200],
                        target=str(rel["target"])[:200],
                        type=str(rel.get("type", "RELATED_TO"))[:50],
                        document_id=document_id
                    )
                    kg_service.create_relation(relation)
                except Exception as e:
                    logger.debug(f"Ошибка сохранения связи: {e}")

            # Сохраняем факты в config_store
            facts = result.get("facts", [])
            if facts:
                doc_data = config_store.get("documents", document_id)
                if doc_data:
                    existing_facts = doc_data.get("extracted_facts", [])
                    existing_facts.extend(facts)
                    doc_data["extracted_facts"] = existing_facts[:50]  # макс 50 фактов
                    config_store.set("documents", document_id, doc_data)

            logger.debug(f"Извлечено из {chunk_id}: {len(result.get('entities',[]))} сущностей, {len(facts)} фактов")

        except Exception as e:
            logger.warning(f"Ошибка extract_and_store для {chunk_id}: {e}")


# Глобальный экземпляр
entity_extractor = EntityExtractor()
