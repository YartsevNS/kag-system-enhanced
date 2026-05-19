"""
Entity Extractor v2.0 — извлечение сущностей через LLM (Neo4j Best Practices).

Ключевые улучшения:
1. Двухпроходное извлечение: быстрый проход (ключевые сущности) + глубокий (связи)
2. Доменная схема: настраиваемые типы сущностей под предметную область
3. Не перегруженные промпты: один проход = одна группа типов сущностей
4. Валидация результатов: проверка качества перед сохранением
5. Контекст-менеджмент: правильный размер чанка для LLM
"""

from typing import Dict, Any, List, Optional
from loguru import logger
import json
import re


class EntityExtractor:
    """Извлекает сущности и факты из чанков через LLM.
    
    Реализует итеративную стратегию Neo4j:
    - Pass 1: Извлечение КЛЮЧЕВЫХ сущностей (имена, названия, даты, суммы)
    - Pass 2: Извлечение СВЯЗЕЙ между сущностями
    - Опциональный Pass 3: Извлечение ДОПОЛНИТЕЛЬНЫХ типов сущностей
    
    Каждый проход использует ОТДЕЛЬНЫЙ промпт — не перегружаем LLM.
    """

    # Доменная схема: группы типов для итеративного извлечения
    DOMAIN_SCHEMA = {
        # Pass 1: Ключевые сущности — извлекаются ВСЕГДА
        "core": {
            "person": "Человек (ФИО, должность)",
            "organization": "Организация (компания, банк, госорган)",
            "date": "Дата (любого формата)",
            "money": "Денежная сумма с валютой",
        },
        # Pass 2: Связи между сущностями
        "relations": {
            "SIGNED_BY": "Документ подписан человеком",
            "BELONGS_TO": "Объект принадлежит организации/человеку",
            "DATED": "Событие датировано",
            "AMOUNT": "Сумма относится к операции",
            "LOCATED_AT": "Организация/человек находится по адресу",
        },
        # Pass 3: Дополнительные типы (опционально, если заданы)
        "extended": {
            "location": "Адрес, местонахождение",
            "document_ref": "Ссылка на документ (номер, серия)",
            "legal_term": "Юридический термин или статья",
        }
    }

    def __init__(self):
        self._llm_url = "http://192.168.50.41:11434"
        self._model = "phi4-mini"
        self._domain_config = dict(self.DOMAIN_SCHEMA)  # Копия, можно менять

    # ============================================================
    # Конфигурация
    # ============================================================

    def _get_graph_config(self):
        """Получить конфигурацию модели графа из админки/config_store."""
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

    def set_domain_schema(self, schema: Dict):
        """Установить пользовательскую доменную схему."""
        self._domain_config = dict(schema)

    # ============================================================
    # Основной метод: двухпроходное извлечение
    # ============================================================

    async def extract_from_chunk(
        self,
        chunk_text: str,
        chunk_id: str,
        document_id: str,
        filename: str = ""
    ) -> Dict[str, Any]:
        """Извлечь сущности из чанка — итеративно, по группам типов.
        
        Pass 1 — Core entities: всегда извлекаем ключевые типы.
        Pass 2 — Relations: связи между найденными сущностями.
        Pass 3 — Extended: дополнительные типы (если есть в схеме).
        
        Returns:
            {"entities": [...], "relations": [...], "facts": [...], "warnings": [...]}
        """
        if not chunk_text or len(chunk_text.strip()) < 20:
            return {"entities": [], "relations": [], "facts": [], "warnings": ["Chunk too short"]}

        cfg = self._get_graph_config()
        model = cfg.get("model", self._model) if cfg else self._model
        llm_url = cfg.get("url", self._llm_url) if cfg else self._llm_url

        all_entities = []
        all_relations = []
        all_facts = []
        warnings = []

        # --- Pass 1: Core entities ---
        core_result = await self._extract_core_entities(
            chunk_text, chunk_id, document_id, filename, model, llm_url
        )
        all_entities.extend(core_result.get("entities", []))
        all_facts.extend(core_result.get("facts", []))
        if core_result.get("warnings"):
            warnings.extend(core_result["warnings"])

        # --- Pass 2: Relations (только если есть сущности) ---
        if all_entities:
            rel_result = await self._extract_relations(
                chunk_text, all_entities, document_id, model, llm_url
            )
            all_relations.extend(rel_result.get("relations", []))
            if rel_result.get("warnings"):
                warnings.extend(rel_result["warnings"])

        # --- Pass 3: Extended entities (опционально) ---
        extended_types = self._domain_config.get("extended", {})
        if extended_types:
            ext_result = await self._extract_extended_entities(
                chunk_text, extended_types, document_id, model, llm_url
            )
            all_entities.extend(ext_result.get("entities", []))
            if ext_result.get("warnings"):
                warnings.extend(ext_result["warnings"])

        # Валидация
        validation_warnings = self._validate_extraction(all_entities, all_relations)
        warnings.extend(validation_warnings)

        return {
            "entities": all_entities,
            "relations": all_relations,
            "facts": all_facts,
            "warnings": warnings
        }

    # ============================================================
    # Pass 1: Ключевые сущности
    # ============================================================

    async def _extract_core_entities(
        self, text: str, chunk_id: str, doc_id: str, filename: str,
        model: str, llm_url: str
    ) -> Dict[str, Any]:
        """Извлечение ключевых сущностей — лёгкий промпт, быстрый ответ.
        
        Neo4j Best Practice: не перегружаем промпт всеми типами сразу.
        Только person, organization, date, money.
        """
        core_types = self._domain_config.get("core", {})
        if not core_types:
            return {"entities": [], "relations": [], "facts": []}

        type_desc = "\n".join([f"  - {t}: {d}" for t, d in core_types.items()])
        sample = text[:1500]  # Берём первые 1500 символов для контекста

        prompt = f"""Извлеки КЛЮЧЕВЫЕ сущности из текста. Верни ТОЛЬКО JSON.

Типы сущностей:
{type_desc}

Текст (первые 1500 символов):
---
{sample}
---

Верни СТРОГО такой JSON без markdown:
{{"entities":[{{"name":"точное имя","type":"тип","confidence":0.0-1.0}}],"facts":["краткий факт"]}}

Правила:
- name: точное значение из текста (не придумывай)
- type: только из списка выше
- confidence: 0.9 если явно указано, 0.7 если косвенно, 0.5 если предположительно
- facts: 1-3 ключевых утверждения из текста
- Если сущностей нет — верни {{"entities":[],"facts":[]}}"""

        return await self._call_llm(prompt, model, llm_url, chunk_id, "core")

    # ============================================================
    # Pass 2: Связи между сущностями
    # ============================================================

    async def _extract_relations(
        self, text: str, existing_entities: List[Dict], doc_id: str,
        model: str, llm_url: str
    ) -> Dict[str, Any]:
        """Извлечение связей между уже найденными сущностями.
        
        Neo4j Best Practice: связи извлекаются ОТДЕЛЬНО от сущностей.
        LLM получает список уже найденных сущностей и ищет связи между ними.
        """
        if not existing_entities:
            return {"relations": [], "warnings": []}

        entity_names = list(set(e["name"] for e in existing_entities))[:30]
        rel_types = self._domain_config.get("relations", {})
        rel_desc = "\n".join([f"  - {t}: {d}" for t, d in rel_types.items()])

        prompt = f"""Найди СВЯЗИ между сущностями в тексте. Верни ТОЛЬКО JSON.

Типы связей:
{rel_desc}

Уже найденные сущности:
{json.dumps(entity_names, ensure_ascii=False)}

Текст:
---
{text[:1000]}
---

Верни СТРОГО такой JSON без markdown:
{{"relations":[{{"source":"сущность1","target":"сущность2","type":"тип связи"}}]}}

Правила:
- source и target ДОЛЖНЫ быть из списка найденных сущностей
- type только из списка типов связей
- Если связей нет — верни {{"relations":[]}}"""

        result = await self._call_llm(prompt, model, llm_url, "relations", "relations")
        return result

    # ============================================================
    # Pass 3: Расширенные сущности (опционально)
    # ============================================================

    async def _extract_extended_entities(
        self, text: str, extended_types: Dict, doc_id: str,
        model: str, llm_url: str
    ) -> Dict[str, Any]:
        """Извлечение дополнительных типов сущностей."""
        type_desc = "\n".join([f"  - {t}: {d}" for t, d in extended_types.items()])

        prompt = f"""Найди ДОПОЛНИТЕЛЬНЫЕ сущности в тексте. Верни ТОЛЬКО JSON.

Типы:
{type_desc}

Текст:
---
{text[:1200]}
---

Верни СТРОГО: {{"entities":[{{"name":"...","type":"...","confidence":0.0-1.0}}]}}
Если нет — {{"entities":[]}}"""

        return await self._call_llm(prompt, model, llm_url, "extended", "extended")

    # ============================================================
    # LLM вызов
    # ============================================================

    async def _call_llm(
        self, prompt: str, model: str, llm_url: str,
        chunk_id: str = "", pass_name: str = ""
    ) -> Dict[str, Any]:
        """Вызвать LLM и распарсить JSON-ответ."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{llm_url}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.05, "max_tokens": 400}
                    },
                    timeout=aiohttp.ClientTimeout(total=180)
                ) as resp:
                    if resp.status != 200:
                        warning = f"LLM вернул {resp.status} (pass={pass_name})"
                        logger.warning(f"Ошибка LLM для {chunk_id}: {warning}")
                        return {"entities": [], "relations": [], "facts": [], "warnings": [warning]}

                    data = await resp.json()
                    response = data.get("response", "")
                    result = self._parse_response(response)
                    
                    if not result.get("entities") and not result.get("relations") and not result.get("facts"):
                        logger.debug(f"Пустой ответ LLM для {chunk_id} (pass={pass_name}): {response[:120]}")
                    
                    return result

        except Exception as e:
            warning = f"{type(e).__name__}: {e}"
            logger.warning(f"Ошибка извлечения ({pass_name}) из {chunk_id}: {warning}")
            return {"entities": [], "relations": [], "facts": [], "warnings": [warning]}

    # ============================================================
    # Парсинг ответа LLM
    # ============================================================

    def _parse_response(self, response: str) -> Dict[str, Any]:
        """Распарсить JSON из ответа LLM.
        
        Устойчив к markdown-обёртке, лишним символам.
        """
        import json as _json
        
        text = response.strip()
        # Убираем markdown-код
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3].strip()

        try:
            data = _json.loads(text)
        except (_json.JSONDecodeError, ValueError):
            # Ищем JSON в тексте
            match = re.search(r'\{[^{}]*\}', text)
            if match:
                try:
                    data = _json.loads(match.group())
                except (_json.JSONDecodeError, ValueError):
                    return {}
            else:
                return {}

        result = {}
        if "entities" in data:
            result["entities"] = [
                {
                    "name": str(e.get("name", ""))[:200],
                    "type": str(e.get("type", "unknown")),
                    "confidence": min(1.0, max(0.0, float(e.get("confidence", 0.7))))
                }
                for e in data["entities"]
                if e.get("name") and len(str(e["name"]).strip()) > 1
            ]
        if "relations" in data:
            result["relations"] = [
                {
                    "source": str(r.get("source", "")),
                    "target": str(r.get("target", "")),
                    "type": str(r.get("type", "RELATED_TO"))
                }
                for r in data["relations"]
                if r.get("source") and r.get("target")
            ]
        if "facts" in data:
            result["facts"] = [str(f)[:300] for f in data["facts"] if f]

        return result

    # ============================================================
    # Валидация
    # ============================================================

    def _validate_extraction(
        self, entities: List[Dict], relations: List[Dict]
    ) -> List[str]:
        """Проверить качество извлечённых данных перед сохранением.
        
        Neo4j Best Practice: валидация на этапе extraction,
        а не post-hoc исправление ошибок в графе.
        """
        warnings = []
        valid_types = set()
        for group in self._domain_config.values():
            valid_types.update(group.keys())

        for e in entities:
            name = e.get("name", "")
            etype = e.get("type", "")
            # Слишком короткое имя — вероятно, мусор
            if len(name.strip()) < 2:
                warnings.append(f"Слишком короткое имя сущности: '{name}'")
            # Неизвестный тип
            if valid_types and etype not in valid_types and etype != "unknown":
                warnings.append(f"Неизвестный тип сущности: '{etype}' для '{name}'")

        return warnings

    # ============================================================
    # Сохранение в граф
    # ============================================================

    async def extract_and_store(
        self, document_id: str, chunk_id: str, chunk_text: str,
        chunk_seq: int = 0, filename: str = ""
    ):
        """Извлечь сущности из чанка и сохранить в Knowledge Graph.
        
        Полный пайплайн:
        1. Извлечение (итеративное, pass 1-3)
        2. Валидация
        3. Сохранение в Neo4j (Domain Graph)
        4. Сохранение в config_store (для UI)
        """
        try:
            from src.indexing.knowledge_graph import kg_service, Entity, Relation

            result = await self.extract_from_chunk(chunk_text, chunk_id, document_id, filename)

            entities = result.get("entities", [])
            relations = result.get("relations", [])

            if not entities:
                return

            # Сохраняем сущности в Domain Graph
            for e in entities:
                entity = Entity(
                    name=e["name"],
                    type=e["type"],
                    chunk_id=chunk_id,
                    document_id=document_id,
                    confidence=e["confidence"],
                    properties=e.get("properties", {})
                )
                kg_service.create_entity(entity)

            # Сохраняем связи
            for r in relations:
                rel = Relation(
                    source=r["source"],
                    target=r["target"],
                    type=r["type"],
                    document_id=document_id
                )
                kg_service.create_relation(rel)

            # Сохраняем в config_store для быстрого доступа из UI
            from src.api.services.config_store import config_store
            key = f"entities_{document_id}"
            existing = config_store.get("entity_cache", key) or {"entities": [], "relations": []}
            existing["entities"].extend(entities)
            existing["relations"].extend(relations)
            config_store.set("entity_cache", key, existing)

            logger.debug(
                f"Извлечено из {chunk_id}: {len(entities)} сущностей, "
                f"{len(relations)} связей"
                + (f", предупреждений: {len(result.get('warnings',[]))}" if result.get("warnings") else "")
            )

        except Exception as e:
            logger.error(f"Ошибка extract_and_store для {chunk_id}: {e}")


# Глобальный экземпляр
entity_extractor = EntityExtractor()
