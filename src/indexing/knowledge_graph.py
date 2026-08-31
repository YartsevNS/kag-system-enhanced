"""
Knowledge Graph Service — Neo4j-сервис для KAG (v2.0, Expert System).

Реализует лучшие практики Neo4j Knowledge Graph Generation:
1. Двухкомпонентная архитектура: Lexical Graph (документы/чанки) + Domain Graph (сущности/связи)
2. MERGE-семантика для автоматической дедупликации сущностей
3. Пост-обработка: entity linking, разрешение дубликатов, валидация
4. Настраиваемая доменная схема сущностей
5. Полнотекстовые индексы для быстрого поиска
6. Составные constraint'ы (name, type) для гарантии уникальности

API: Bolt-драйвер neo4j
Схема:
  Document {id, filename, metadata, created_at, updated_at}
      └─[:HAS_CHUNK]→ Chunk {id, text_preview, chunk_seq, updated_at}
                          └─[:MENTIONS]→ Entity {name, type, confidence, properties, source_docs[], updated_at}
                                            └─[:RELATED_TO|SIGNED_BY|DATED|AMOUNT|...]→ Entity
"""

from typing import Dict, Any, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from loguru import logger
import json
import re


# ============================================================
# Data Classes
# ============================================================

@dataclass
class Entity:
    """Сущность, извлечённая из документа.
    
    name + type образуют композитный ключ (MERGE гарантирует уникальность).
    source_docs отслеживает, из каких документов извлечена сущность.
    """
    name: str
    type: str  # Из доменной схемы: person, organization, date, money, location, document_ref, legal_term, ...
    chunk_id: str = ""
    document_id: str = ""
    confidence: float = 1.0
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Relation:
    """Связь между сущностями в Domain Graph.
    
    Типы связей определяются доменной схемой.
    Связи MERGE'ятся по (source, target, type) — дубликаты не создаются.
    """
    source: str       # name сущности-источника
    target: str       # name сущности-цели
    type: str         # Тип связи: MENTIONS, RELATED_TO, SIGNED_BY, DATED, AMOUNT, BELONGS_TO, LOCATED_AT
    document_id: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionResult:
    """Результат извлечения сущностей — для валидации."""
    entities: List[Entity] = field(default_factory=list)
    relations: List[Relation] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ============================================================
# Neo4j 5.x совместимые индексы — без IF NOT EXISTS
# ============================================================
def _safe_create_index(session, label: str, prop: str, index_name: str):
    """Создать индекс в Neo4j, игнорируя ошибку если уже существует."""
    try:
        session.run(f"CREATE INDEX {index_name} FOR (n:{label}) ON (n.{prop})")
    except Exception:
        pass  # Индекс уже существует


def _safe_create_text_index(session, index_name: str, label: str, prop: str):
    """Создать текстовый индекс (Neo4j 5.x)."""
    try:
        session.run(f"CREATE TEXT INDEX {index_name} FOR (n:{label}) ON (n.{prop})")
    except Exception:
        pass


def _safe_create_constraint(session, constraint_name: str, label: str, prop: str):
    """Создать constraint уникальности."""
    try:
        session.run(f"CREATE CONSTRAINT {constraint_name} FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")
    except Exception:
        pass


# ============================================================
# Knowledge Graph Service
# ============================================================

class KnowledgeGraphService:
    """Экспертный сервис графа знаний на Neo4j.
    
    Реализует двухкомпонентную архитектуру:
    - Lexical Graph: Document → Chunk (структура документов)
    - Domain Graph: Entity → Entity (извлечённые знания)
    
    Связь между компонентами: Chunk -[:MENTIONS]-> Entity
    """

    # Доменная схема по умолчанию — универсальные типы сущностей
    DEFAULT_DOMAIN_SCHEMA = {
        "person": {"label": "🧑 Человек", "color": "#7170ff"},
        "organization": {"label": "🏢 Организация", "color": "#60a5fa"},
        "date": {"label": "📅 Дата", "color": "#10b981"},
        "money": {"label": "💰 Сумма", "color": "#f59e0b"},
        "location": {"label": "📍 Место", "color": "#f87171"},
        "document_ref": {"label": "📄 Документ", "color": "#a78bfa"},
        "legal_term": {"label": "⚖️ Юр. термин", "color": "#fb923c"},
    }

    # Типы связей по умолчанию
    DEFAULT_RELATION_TYPES = [
        "RELATED_TO",   # Общая связь
        "SIGNED_BY",    # Документ подписан
        "DATED",        # Датировано
        "AMOUNT",       # На сумму
        "BELONGS_TO",   # Принадлежит
        "LOCATED_AT",   # Находится по адресу
        "MENTIONS",     # Упоминается в чанке (системная связь)
    ]

    def __init__(self, uri: str = "bolt://neo4j:7687", user: str = "neo4j", password: str = None):
        import os
        self._uri = uri
        self._user = user
        # Пароль из env NEO4J_PASSWORD (генерируется deploy.sh). Fallback — старый дефолт.
        self._password = password or os.environ.get("NEO4J_PASSWORD", "") or "kagneo4j2026"
        self._driver = None
        self._initialized = False
        self._domain_schema = dict(self.DEFAULT_DOMAIN_SCHEMA)
        # План entity resolution (заполняется в resolve_duplicate_entities)
        self._resolution_plan = {}
        # Кандидаты на LLM-верификацию: пары в «серой зоне» embedding-сходства
        # (0.85-0.95) — не сливаем автоматически, спрашиваем LLM
        self._resolution_candidates = []

    # ============================================================
    # Инициализация
    # ============================================================

    @property
    def driver(self):
        """Ленивая инициализация Neo4j драйвера."""
        if self._driver is None:
            try:
                from neo4j import GraphDatabase
                self._driver = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
                self._driver.verify_connectivity()
                logger.info(f"Neo4j подключён: {self._uri}")
                self._init_schema()
            except Exception as e:
                logger.warning(f"Neo4j недоступен: {e}")
                self._driver = None
        return self._driver

    def _init_schema(self):
        """Инициализировать схему: индексы, constraints, полнотекстовые индексы.
        
        Neo4j Best Practice: индексы критичны для производительности
        графовых запросов. Без индексов каждый MATCH сканирует весь граф.
        """
        if not self.driver or self._initialized:
            return
        try:
            with self.driver.session() as session:
                # --- Lexical Graph индексы ---
                _safe_create_index(session, "Document", "id", "idx_doc_id")
                _safe_create_index(session, "Chunk", "id", "idx_chunk_id")
                _safe_create_constraint(session, "cst_doc_unique", "Document", "id")
                _safe_create_constraint(session, "cst_chunk_unique", "Chunk", "id")

                # --- Domain Graph индексы ---
                _safe_create_index(session, "Entity", "name", "idx_entity_name")
                _safe_create_index(session, "Entity", "type", "idx_entity_type")
                # Composite (name, type) уникальность НЕ создаём: NODE KEY —
                # Enterprise-функция Neo4j, в Community (neo4j:5.26-community)
                # она не поддерживается и при попытке создать падает
                # ConstraintCreationFailed, засоряя логи. Дедупликация и так
                # работает через MERGE (e:Entity {name, type}) + индекс на name.

                # Полнотекстовый индекс для поиска сущностей
                _safe_create_text_index(session, "txt_entity_name", "Entity", "name")

                logger.info("Neo4j схема инициализирована (Lexical + Domain)")
                self._initialized = True
        except Exception as e:
            logger.warning(f"Ошибка инициализации схемы Neo4j: {e}")

    # ============================================================
    # Lexical Graph: Document → Chunk
    # ============================================================

    def create_document_node(self, document_id: str, filename: str, metadata: Dict = None):
        """Создать/обновить узел документа в Lexical Graph.
        
        Neo4j Best Practice: используем MERGE для идемпотентности.
        """
        if not self.driver:
            return
        try:
            with self.driver.session() as session:
                session.run(
                    """
                    MERGE (d:Document {id: $id})
                    SET d.filename = $filename,
                        d.metadata = $metadata,
                        d.updated_at = datetime()
                    """,
                    id=document_id,
                    filename=filename,
                    metadata=json.dumps(metadata, ensure_ascii=False) if metadata else "{}",
                )
        except Exception as e:
            logger.warning(f"Ошибка создания узла документа: {e}")

    def create_chunk_node(self, chunk_id: str, document_id: str, text: str, chunk_seq: int = 0):
        """Создать/обновить узел чанка и связь HAS_CHUNK с документом.
        
        Храним первые 500 символов текста в графе — для быстрого предпросмотра.
        """
        if not self.driver:
            return
        try:
            with self.driver.session() as session:
                session.run(
                    """
                    MERGE (c:Chunk {id: $chunk_id})
                    SET c.text_preview = $text_preview,
                        c.chunk_seq = $chunk_seq,
                        c.updated_at = datetime()
                    WITH c
                    MATCH (d:Document {id: $doc_id})
                    MERGE (d)-[:HAS_CHUNK]->(c)
                    """,
                    chunk_id=chunk_id,
                    doc_id=document_id,
                    text_preview=text[:500],
                    chunk_seq=chunk_seq
                )
        except Exception as e:
            logger.warning(f"Ошибка создания узла чанка: {e}")

    # ============================================================
    # Domain Graph: Entity → Entity
    # ============================================================

    def create_entity(self, entity: Entity):
        """Создать/обновить сущность в Domain Graph.
        
        Neo4j Best Practice: MERGE по (name, type) = автоматическая дедупликация.
        Сущность «Иванов И.И.» типа person, найденная в двух документах,
        будет существовать в одном экземпляре с обновлённым source_docs.
        
        Атрибут source_docs хранит список ID документов, где встречается сущность.
        properties могут содержать description — краткое описание сущности
        (используется entity resolution и community detection).
        """
        if not self.driver:
            return
        try:
            with self.driver.session() as session:
                session.run(
                    """
                    MERGE (e:Entity {name: $name, type: $type})
                    SET e.confidence = CASE 
                            WHEN e.confidence IS NULL THEN $confidence
                            ELSE (e.confidence + $confidence) / 2.0  // Усредняем confidence
                        END,
                        e.properties = $properties,
                        e.updated_at = datetime()
                    // Если есть description в properties — кладём и в отдельное поле
                    // (удобнее для поиска и resolution)
                    WITH e, $description AS desc
                    FOREACH (_ IN CASE WHEN desc IS NOT NULL AND desc <> '' THEN [1] ELSE [] END |
                        SET e.description = desc
                    )
                    // Добавляем doc_id в source_docs если ещё не там
                    FOREACH (_ IN CASE WHEN NOT $doc_id IN coalesce(e.source_docs, []) THEN [1] ELSE [] END |
                        SET e.source_docs = coalesce(e.source_docs, []) + $doc_id
                    )
                    WITH e
                    MATCH (c:Chunk {id: $chunk_id})
                    MERGE (c)-[:MENTIONS]->(e)
                    """,
                    name=entity.name,
                    type=entity.type,
                    confidence=entity.confidence,
                    properties=json.dumps(entity.properties, ensure_ascii=False) if entity.properties else "{}",
                    description=entity.properties.get("description", ""),
                    doc_id=entity.document_id,
                    chunk_id=entity.chunk_id
                )
        except Exception as e:
            logger.warning(f"Ошибка создания сущности '{entity.name}': {e}")

    def create_relation(self, rel: Relation):
        """Создать связь между сущностями в Domain Graph.
        
        MERGE по (source, target, type) предотвращает дублирование связей.
        """
        if not self.driver:
            return
        try:
            with self.driver.session() as session:
                # Динамическое имя связи — безопасно, т.к. тип из доменной схемы
                safe_type = rel.type.replace("`", "").replace(" ", "_")
                session.run(
                    f"""
                    MATCH (a:Entity {{name: $source}})
                    MATCH (b:Entity {{name: $target}})
                    MERGE (a)-[:`{safe_type}`]->(b)
                    """,
                    source=rel.source,
                    target=rel.target
                )
        except Exception as e:
            logger.warning(f"Ошибка создания связи {rel.type}: {e}")

    # ============================================================
    # Батч-запись (UNWIND) — вместо N одиночных create_entity/relation
    # ============================================================

    def batch_create_entities(self, entities: List["Entity"]) -> int:
        """Записать пачку сущностей ОДНИМ UNWIND-запросом.

        Логика та же, что у create_entity (MERGE по name+type, усреднение
        confidence, source_docs, MENTIONS к Chunk), но за один round-trip.
        """
        if not self.driver or not entities:
            return 0
        batch = []
        for e in entities:
            props = e.properties or {}
            batch.append({
                "name": e.name,
                "type": e.type,
                "confidence": float(e.confidence or 0.5),
                "properties": json.dumps(props, ensure_ascii=False),
                "description": props.get("description", ""),
                "doc_id": e.document_id,
                "chunk_id": e.chunk_id,
            })
        try:
            from src.api.services.config_store import config_store
            _cfg = config_store.get("neo4j", "config") or {}
            _bs = int(_cfg.get("batch_size", 100) or 100)
            _batch_enabled = bool(_cfg.get("batch_enabled", True))
        except Exception:
            _bs, _batch_enabled = 100, True
        if not _batch_enabled:
            # Откат к поодиночной записи (create_entity)
            saved = 0
            for e in entities:
                self.create_entity(e)
                saved += 1
            return saved
        try:
            with self.driver.session() as session:
                for i in range(0, len(batch), _bs):
                    session.run(
                        """
                        UNWIND $batch AS e
                        MERGE (n:Entity {name: e.name, type: e.type})
                        SET n.confidence = CASE
                                WHEN n.confidence IS NULL THEN e.confidence
                                ELSE (n.confidence + e.confidence) / 2.0
                            END,
                            n.properties = e.properties,
                            n.updated_at = datetime()
                        FOREACH (_ IN CASE WHEN e.description IS NOT NULL AND e.description <> '' THEN [1] ELSE [] END |
                            SET n.description = e.description
                        )
                        FOREACH (_ IN CASE WHEN NOT e.doc_id IN coalesce(n.source_docs, []) THEN [1] ELSE [] END |
                            SET n.source_docs = coalesce(n.source_docs, []) + e.doc_id
                        )
                        WITH n, e
                        MATCH (c:Chunk {id: e.chunk_id})
                        MERGE (c)-[:MENTIONS]->(n)
                        """,
                        batch=batch[i:i + _bs],
                    )
            return len(batch)
        except Exception as e:
            logger.warning(f"Ошибка batch_create_entities ({len(batch)}): {e}")
            return 0

    def batch_create_relations(self, rels: List["Relation"]) -> int:
        """Записать пачку связей UNWIND-запросами (по одному на тип связи).

        Динамическое имя связи нельзя задать внутри UNWIND без APOC, поэтому
        группируем по safe_type и делаем один запрос на тип.
        """
        if not self.driver or not rels:
            return 0
        by_type: Dict[str, list] = {}
        for r in rels:
            safe = r.type.replace("`", "").replace(" ", "_")
            by_type.setdefault(safe, []).append({"source": r.source, "target": r.target})
        total = 0
        try:
            from src.api.services.config_store import config_store
            _cfg = config_store.get("neo4j", "config") or {}
            _bs = int(_cfg.get("batch_size", 100) or 100)
            _batch_enabled = bool(_cfg.get("batch_enabled", True))
        except Exception:
            _bs, _batch_enabled = 100, True
        if not _batch_enabled:
            saved = 0
            for r in rels:
                self.create_relation(r)
                saved += 1
            return saved
        try:
            with self.driver.session() as session:
                for safe_type, batch in by_type.items():
                    for i in range(0, len(batch), _bs):
                        session.run(
                            f"""
                            UNWIND $batch AS r
                            MATCH (a:Entity {{name: r.source}})
                            MATCH (b:Entity {{name: r.target}})
                            MERGE (a)-[:`{safe_type}`]->(b)
                            """,
                            batch=batch[i:i + _bs],
                        )
                        total += len(batch[i:i + _bs])
            return total
        except Exception as e:
            logger.warning(f"Ошибка batch_create_relations ({len(rels)}): {e}")
            return 0

    # ============================================================
    # Пост-обработка: Entity Linking и Dedup (Neo4j Best Practice)
    # ============================================================

    def post_process_entities(self, document_id: str = None) -> Dict[str, Any]:
        """Пост-обработка графа: слияние дубликатов, нормализация.
        
        Этапы:
        1. Слияние сущностей с одинаковым именем, разным регистром
        2. Создание связей RELATED_TO между сущностями одного документа
        3. Обновление source_docs у общих сущностей
        
        Returns:
            Dict со статистикой: merged_count, linked_count
        """
        if not self.driver:
            return {"merged": 0, "linked": 0, "error": "Neo4j не подключён"}
        
        result = {"merged": 0, "linked": 0}
        try:
            with self.driver.session() as session:
                # 1. Слияние по нормализованному имени (lowercase + trim)
                #    Сущности "Иванов" и "иванов" → один узел
                merge_result = session.run("""
                    MATCH (e1:Entity), (e2:Entity)
                    WHERE id(e1) < id(e2)
                      AND toLower(trim(e1.name)) = toLower(trim(e2.name))
                      AND e1.type = e2.type
                    WITH e1, e2
                    // Переносим все связи с e2 на e1
                    OPTIONAL MATCH (e2)<-[r_in]-()
                    OPTIONAL MATCH (e2)-[r_out]->()
                    CALL {
                        WITH e1, e2, r_in, r_out
                        FOREACH (r IN r_in | 
                            MERGE (startNode(r))-[new:MENTIONS]->(e1)
                            SET new = properties(r)
                        )
                        FOREACH (r IN r_out |
                            MERGE (e1)-[new:RELATED_TO]->(endNode(r))
                            SET new = properties(r)
                        )
                    }
                    // Объединяем source_docs
                    SET e1.source_docs = apoc.coll.union(
                        coalesce(e1.source_docs, []), 
                        coalesce(e2.source_docs, [])
                    )
                    DETACH DELETE e2
                    RETURN count(e2) as merged
                """)
                rec = merge_result.single()
                if rec:
                    result["merged"] = rec["merged"]

                # 2. Создание связей между сущностями одного документа
                if document_id:
                    link_result = session.run("""
                        MATCH (d:Document {id: $doc_id})-[:HAS_CHUNK]->(c:Chunk)-[:MENTIONS]->(e1:Entity)
                        MATCH (d)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e2:Entity)
                        WHERE id(e1) < id(e2) AND e1.type <> e2.type
                        MERGE (e1)-[:RELATED_TO {doc_id: $doc_id}]->(e2)
                        RETURN count(*) as linked
                    """, doc_id=document_id)
                    rec = link_result.single()
                    if rec:
                        result["linked"] = rec["linked"]

                logger.info(f"Пост-обработка: слито {result['merged']}, связано {result['linked']}")
        except Exception as e:
            logger.warning(f"Ошибка пост-обработки: {e}")
            result["error"] = str(e)
        return result

    def deduplicate_entities_by_name(self) -> int:
        """Упрощённый dedup для Neo4j Community (без APOC).
        
        Ищет сущности с одинаковым нормализованным именем и типом,
        оставляет одну с наибольшим confidence, удаляет дубликаты.
        
        Returns:
            Количество удалённых дубликатов.
        """
        if not self.driver:
            return 0
        try:
            with self.driver.session() as session:
                result = session.run("""
                    MATCH (e1:Entity), (e2:Entity)
                    WHERE id(e1) < id(e2)
                      AND toLower(trim(e1.name)) = toLower(trim(e2.name))
                      AND e1.type = e2.type
                    WITH e1, e2, 
                         coalesce(e1.confidence, 0) as c1,
                         coalesce(e2.confidence, 0) as c2
                    // Оставляем сущность с бОльшим confidence
                    WITH CASE WHEN c1 >= c2 THEN e1 ELSE e2 END AS keeper,
                         CASE WHEN c1 >= c2 THEN e2 ELSE e1 END AS duplicate
                    // Переносим MENTIONS связи на keeper
                    OPTIONAL MATCH (c:Chunk)-[r:MENTIONS]->(duplicate)
                    FOREACH (_ IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END |
                        MERGE (c)-[:MENTIONS]->(keeper)
                    )
                    // Объединяем source_docs
                    SET keeper.source_docs = [x IN coalesce(keeper.source_docs, []) + coalesce(duplicate.source_docs, []) | x]
                    WITH keeper, duplicate, c
                    DELETE r
                    DETACH DELETE duplicate
                    RETURN count(duplicate) as removed
                """)
                rec = result.single()
                removed = rec["removed"] if rec else 0
                if removed > 0:
                    logger.info(f"Dedup: удалено {removed} дубликатов")
                return removed
        except Exception as e:
            logger.warning(f"Ошибка dedup: {e}")
            return 0

    def _norm_name(self, s: str) -> str:
        """Нормализация имени сущности для entity resolution.
        Нижний регистр, NFKC, убрать пунктуацию/лишние пробелы."""
        import unicodedata
        s = unicodedata.normalize("NFKC", s or "")
        s = s.lower().strip()
        s = re.sub(r"[.,;:!?()\"'«»„“”\-–—/\\|]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _extract_year(self, norm: str):
        """Извлечь год из нормализованного имени документа, если он в конце.
        Возвращает (base_without_year, year) или None.
        Год должен быть правдоподобным (1900-2100) — иначе «исо мэк 18033»
        принял бы «8033» за год."""
        m = re.fullmatch(r"(.+?)[\s:–—-]*(\d{4})$", norm)
        if not m:
            return None
        year = int(m.group(2))
        if not (1900 <= year <= 2100):
            return None
        return m.group(1).strip(), year

    def resolve_duplicate_entities(self, threshold: float = 0.90) -> Dict[str, int]:
        """Entity Resolution: слияние сущностей, ссылающихся на один реальный объект.

        Исследование (2025-2026): LLM извлекает surface forms — «Банк России»,
        «ЦБ», «ЦБ РФ», «регулятор» — как разные узлы (34% дублей в типичном
        графе). Это рвёт связи и убивает точность. См. docs/guides/graph-precision-architecture.md.

        Сигналы (по Duk Lee / modernData101):
        1. Lexical: нормализация (lower, trim, убрать пунктуацию) — точное совпадение
        2. Embedding similarity: косинусная близость эмбеддингов имён > threshold
        3. Graph topology: ≥2 общих соседей — вероятно, тот же узел

        Слияние: канонический узел (с большим confidence / большим source_docs),
        остальные — как aliases (свойство aliases на каноническом узле),
        связи MENTIONS переносятся.

        Returns:
            {"merged": N, "aliased": M}
        """
        if not self.driver:
            return {"merged": 0, "aliased": 0}
        result = {"merged": 0, "aliased": 0}
        try:
            # ── 1. Собираем все сущности с нормализованными именами ──────────
            with self.driver.session() as session:
                rows = list(session.run(
                    "MATCH (e:Entity) RETURN e.name AS name, e.type AS type, "
                    "e.confidence AS confidence, id(e) AS node_id, "
                    "size(coalesce(e.source_docs, [])) AS doc_count, "
                    "coalesce(e.aliases, []) AS aliases"
                ))

            import unicodedata
            def _norm(s: str) -> str:
                return self._norm_name(s)

            # Нормализуем все имена
            entities = []
            for r in rows:
                entities.append({
                    "node_id": r["node_id"],
                    "name": r["name"],
                    "norm": _norm(r["name"]),
                    "type": r["type"],
                    "confidence": r["confidence"] or 0,
                    "doc_count": r["doc_count"],
                    "aliases": list(r["aliases"] or []),
                })

            # ── 2. Blocking по нормализованному имени (lexical) ───────────────
            # Группируем по (norm, type) — это дешёвый точный сигнал
            from collections import defaultdict
            groups = defaultdict(list)
            for e in entities:
                groups[(e["norm"], e["type"])].append(e)

            # Планируем слияния: {node_id: keeper_node_id}
            # self._resolution_plan — общий план, дополняется embedding-шагом
            self._resolution_plan = {}
            merge_plan = self._resolution_plan
            for (norm, etype), group in groups.items():
                if len(group) < 2 or not norm:
                    continue
                # Канонический: максимальный confidence, при равенстве — больше doc_count
                keeper = max(group, key=lambda e: (e["confidence"], e["doc_count"]))
                for dup in group:
                    if dup["node_id"] != keeper["node_id"]:
                        merge_plan[dup["node_id"]] = keeper["node_id"]
                        result["aliased"] += 1

            # ── 3. Embedding similarity для несовпадающих имён ────────────────
            # Пробуем получить embedding-клиент (тот же, что для чанков).
            # Для пар (norm,type) с похожими нормализованными именами проверяем
            # косинусную близость. Порог 0.90.
            try:
                from src.indexing.embeddings_service import embeddings_service
                # Только сущности, не попавшие в lexical-группы
                unmerged = [e for e in entities if e["node_id"] not in merge_plan and e["norm"]]
                if len(unmerged) >= 2:
                    import asyncio
                    # Не блокируем event loop: embedding — async. resolve_duplicate_entities
                    # может вызываться из asyncio.to_thread (worker) ИЛИ из async-контекста
                    # (тесты/скрипты). asyncio.run() внутри работающего loop падает
                    # ("This event loop is already running") — поэтому создаём СВОЙ
                    # event loop в отдельном потоке.
                    def _run_embed_plan():
                        loop = asyncio.new_event_loop()
                        try:
                            loop.run_until_complete(
                                self._embed_entities_resolution(unmerged, threshold)
                            )
                        finally:
                            loop.close()
                    import threading
                    _t = threading.Thread(target=_run_embed_plan, daemon=True)
                    _t.start()
                    _t.join(timeout=120)
            except Exception as e:
                logger.warning(f"[resolution] Embedding similarity пропущен: {e}")

            # ── 3b. LLM-верификация пар из «серой зоны» ─────────────────────
            # Кандидаты собрал embed-шаг (0.85-0.95). Спрашиваем LLM «это один
            # объект?» — решает случаи «ЦБ»=«Банк России» (алиас) vs
            # «Сбербанк»=«Банк России» (разные), которые embedding не различает.
            if self._resolution_candidates:
                # Лимит: не больше N кандидатов за прогон (защита от перерасхода)
                MAX_LLM_CANDIDATES = 40
                candidates = self._resolution_candidates[:MAX_LLM_CANDIDATES]
                logger.info(
                    f"[resolution] LLM-верификация: {len(candidates)} пар "
                    f"(серая зона 0.85-0.95)"
                )
                try:
                    import asyncio, threading
                    def _run_verify():
                        loop = asyncio.new_event_loop()
                        try:
                            loop.run_until_complete(self._verify_pairs_llm(candidates))
                        finally:
                            loop.close()
                    _vt = threading.Thread(target=_run_verify, daemon=True)
                    _vt.start()
                    _vt.join(timeout=180)
                except Exception as e:
                    logger.warning(f"[resolution] LLM-верификация пропущена: {e}")
                # ВСЕ кандидаты (и подтверждённые LLM, и нет) → на модерацию
                # админу: он видит в админке и решает, применять ли.
                try:
                    self.save_pending_pairs(candidates)
                except Exception as e:
                    logger.warning(f"[resolution] save_pending_pairs: {e}")
                self._resolution_candidates = []

            # ── 4. Применяем слияния в Neo4j ──────────────────────────────────
            if merge_plan:
                # Transitive closure: если keeper сам в merge_plan как dup
                # (цепочка «ЦБ» → «ЦБ РФ» → «Банк России»), перенаправляем
                # на корневого канонического.
                def _root(node_id: str) -> str:
                    seen = set()
                    cur = node_id
                    while cur in merge_plan and cur not in seen:
                        seen.add(cur)
                        cur = merge_plan[cur]
                    return cur
                # Перестраиваем план: каждый dup → корневой keeper
                final_plan = {}
                for dup_id, keeper_id in merge_plan.items():
                    root = _root(keeper_id)
                    if root != dup_id:
                        final_plan[dup_id] = root
                merge_plan = final_plan

                with self.driver.session() as session:
                    for dup_id, keeper_id in merge_plan.items():
                        try:
                            session.run(
                                """
                                MATCH (keeper:Entity) WHERE id(keeper) = $keeper_id
                                MATCH (dup:Entity) WHERE id(dup) = $dup_id
                                // Добавляем dup в aliases канонического (без дублей)
                                SET keeper.aliases = [x IN (coalesce(keeper.aliases, []) + [dup.name]) WHERE NOT x IN coalesce(keeper.aliases, [])]
                                WITH keeper, dup
                                // Переносим MENTIONS связи с dup на keeper
                                OPTIONAL MATCH (c:Chunk)-[r:MENTIONS]->(dup)
                                FOREACH (_ IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END |
                                    MERGE (c)-[:MENTIONS]->(keeper)
                                )
                                WITH keeper, dup
                                // Объединяем source_docs
                                SET keeper.source_docs = [x IN coalesce(keeper.source_docs, []) + coalesce(dup.source_docs, []) | x]
                                WITH keeper, dup
                                // Переносим остальные связи
                                OPTIONAL MATCH (dup)-[r2]->(t)
                                FOREACH (_ IN CASE WHEN t IS NOT NULL THEN [1] ELSE [] END |
                                    MERGE (keeper)-[nr:RELATED_TO]->(t)
                                    SET nr = properties(r2)
                                )
                                WITH keeper, dup
                                OPTIONAL MATCH (s)-[r3]->(dup)
                                FOREACH (_ IN CASE WHEN s IS NOT NULL THEN [1] ELSE [] END |
                                    MERGE (s)-[nr2:RELATED_TO]->(keeper)
                                    SET nr2 = properties(r3)
                                )
                                DETACH DELETE dup
                                """,
                                keeper_id=keeper_id,
                                dup_id=dup_id,
                            )
                            result["merged"] += 1
                        except Exception as e:
                            logger.warning(f"[resolution] Ошибка слияния {dup_id}→{keeper_id}: {e}")
            logger.info(f"Entity resolution: слито {result['merged']}, aliases {result['aliased']}")
        except Exception as e:
            logger.warning(f"Ошибка entity resolution: {e}")
        return result

    def link_document_versions(self) -> Dict[str, int]:
        """Связать версии документов цепочкой SUPERSEDED_BY.

        Версии стандартов/приказов/законов — РАЗНЫЕ узлы (не сливаются в
        resolve_duplicate_entities). Здесь они связываются по времени:
        (ИСО/МЭК 18033-1:2005)-[:SUPERSEDED_BY]->(ИСО/МЭК 18033-1:2015)
        (ИСО/МЭК 18033-1:2015)-[:SUPERSEDED_BY]->(ИСО/МЭК 18033-1:2021)
        Последняя версия помечается is_current = true.

        Группировка: нормализованное имя без года → сущности одного документа.
        Год извлекается из суффикса (ISO :YYYY, ГОСТ —YYYY). Если в группе
        только одна версия — она просто помечается is_current.

        Returns:
            {"chains": N, "versions": M, "current": K}
        """
        if not self.driver:
            return {"chains": 0, "versions": 0, "current": 0}
        result = {"chains": 0, "versions": 0, "current": 0}
        try:
            with self.driver.session() as session:
                rows = list(session.run(
                    "MATCH (e:Entity) WHERE e.type = 'document_ref' "
                    "RETURN e.name AS name, id(e) AS node_id"
                ))

            # Группируем по базовому имени (без года)
            from collections import defaultdict
            groups = defaultdict(list)  # base -> [(year, node_id, name)]
            for r in rows:
                name = r["name"]
                norm = self._norm_name(name)
                parsed = self._extract_year(norm)
                if not parsed:
                    continue
                base, year = parsed
                # Фильтр: база должна быть осмысленной (не просто «№ 749»)
                if len(base) < 3:
                    continue
                groups[base].append((year, r["node_id"], name))

            with self.driver.session() as session:
                for base, items in groups.items():
                    # Сортируем по году, отбрасываем дубли (одинаковый год+имя)
                    seen = set()
                    unique = []
                    for year, node_id, name in items:
                        key = (year, name)
                        if key in seen:
                            continue
                        seen.add(key)
                        unique.append((year, node_id, name))
                    unique.sort(key=lambda x: x[0])
                    if len(unique) < 2:
                        # Одна версия — просто пометить актуальной
                        try:
                            session.run(
                                "MATCH (e:Entity) WHERE id(e) = $id SET e.is_current = true",
                                id=unique[0][1],
                            )
                            result["current"] += 1
                        except Exception:
                            pass
                        continue
                    # Цепочка: старая SUPERSEDED_BY новая
                    for i in range(len(unique) - 1):
                        old_year, old_id, old_name = unique[i]
                        new_year, new_id, new_name = unique[i + 1]
                        try:
                            session.run(
                                """
                                MATCH (a:Entity) WHERE id(a) = $aid
                                MATCH (b:Entity) WHERE id(b) = $bid
                                MERGE (a)-[:SUPERSEDED_BY {old_year: $oy, new_year: $ny}]->(b)
                                """,
                                aid=old_id, bid=new_id, oy=old_year, ny=new_year,
                            )
                            result["chains"] += 1
                        except Exception as e:
                            logger.warning(f"[versions] Ошибка связи {old_name}→{new_name}: {e}")
                    # Последняя — актуальная
                    try:
                        session.run(
                            "MATCH (e:Entity) WHERE id(e) = $id SET e.is_current = true",
                            id=unique[-1][1],
                        )
                        result["current"] += 1
                    except Exception:
                        pass
                    result["versions"] += len(unique)
            if result["chains"]:
                logger.info(f"Версии документов: цепочек {result['chains']}, версий {result['versions']}")
        except Exception as e:
            logger.warning(f"Ошибка link_document_versions: {e}")
        return result

    async def _embed_entities_resolution(self, entities: List[Dict], threshold: float):
        """Вспомогательный шаг: embedding-сходство имён для entity resolution.

        Ищем пары сущностей одного типа с косинусной близостью > threshold
        и дополняем merge_plan (через self._resolution_plan).
        """
        try:
            from src.indexing.embeddings_service import embeddings_service
            from src.llm.embeddings import EmbeddingClient
            await embeddings_service.initialize()
            client = embeddings_service._embedding_client
            if client is None:
                return
            # Группируем по типу
            from collections import defaultdict
            by_type = defaultdict(list)
            for e in entities:
                by_type[e["type"]].append(e)
            for etype, group in by_type.items():
                if len(group) < 2:
                    continue
                # ⚠️ ПОЛИТИКА ПО ТИПАМ (глобально):
                # - legal_term: НЕ сливаем по embedding вообще — термины почти
                #   никогда не бывают алиасами («зашифрование» ≠ «расшифрование»,
                #   «5.4» ≠ «5.4.1», «А.1» ≠ «А.2»). Известные синонимы — через
                #   таблицу entity_aliases (apply_alias_pairs).
                # - document_ref: НЕ сливаем по embedding (серия/часть/версия —
                #   разные; версии связываются SUPERSEDED_BY).
                # - organization/person: сливаем с осторожностью (порог 0.95),
                #   серые пары → LLM-верификация.
                if etype in ("legal_term", "document_ref"):
                    continue
                # Батчим эмбеддинги имён
                names = [e["norm"] for e in group]
                try:
                    vecs = await client.generate_batch(names, batch_size=8)
                except Exception:
                    continue
                import math
                def _cos(a, b):
                    dot = sum(x * y for x, y in zip(a, b))
                    na = math.sqrt(sum(x * x for x in a))
                    nb = math.sqrt(sum(x * x for x in b))
                    if na == 0 or nb == 0:
                        return 0.0
                    return dot / (na * nb)
                # Проверяем пары (O(n²), но n — сущности одного типа, обычно мало)
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        ei, ej = group[i], group[j]
                        if ei["node_id"] == ej["node_id"]:
                            continue
                        # ЛОЖНОПОЛОЖИТЕЛЬНАЯ ЗАЩИТА: короткие имена организаций
                        # («Банк России» vs «Сбербанк») имеют близкие эмбеддинги —
                        # оба «банковские». Порог для embedding-слияния поднимаем
                        # до 0.95, и только для имён >= 4 символов.
                        # Слишком агрессивное слияние (0.85) склеило Сбербанк
                        # с Банком России — это недопустимо.
                        if len(ei["name"]) < 4 or len(ej["name"]) < 4:
                            continue
                        # ⚠️ document_ref: НЕ сливаем версии — это отдельные узлы,
                        # связанные цепочкой SUPERSEDED_BY (link_document_versions).
                        # «ИСО/МЭК 18033-1:2005» и «ИСО/МЭК 18033-1:2015» — редакции
                        # одного документа, но каждая хранится отдельным узлом с
                        # своим годом. Серия «ИСО/МЭК 18033» и часть «18033-1» —
                        # РАЗНЫЕ документы, их тоже не сливаем.
                        if ei["type"] == "document_ref" and ej["type"] == "document_ref":
                            _n1, _n2 = ei["norm"], ej["norm"]
                            _is_ver = False
                            # 1) Подстрока: «18033-1:2005» ⊃ «18033-1» — суффикс год
                            if _n1 in _n2 or _n2 in _n1:
                                _short, _long = (_n1, _n2) if _n1 in _n2 else (_n2, _n1)
                                _suffix = _long[len(_short):].strip()
                                _is_ver = bool(re.fullmatch(r"[\s:–—-]*\d{4}", _suffix))
                            # 2) Общий префикс + разные годы в конце:
                            #    «ГОСТ Р 34.12—2015» vs «ГОСТ Р 34.12—2018» —
                            #    нормализация убрала тире → «гост р 34 12 2015»
                            #    vs «гост р 34 12 2018». Общий префикс до года.
                            if not _is_ver:
                                _m1 = re.fullmatch(r"(.+?)[\s:–—-]*(\d{4})$", _n1)
                                _m2 = re.fullmatch(r"(.+?)[\s:–—-]*(\d{4})$", _n2)
                                if _m1 and _m2:
                                    if _m1.group(1).strip() == _m2.group(1).strip():
                                        _is_ver = True
                            # Версии — пропускаем (не сливаем, свяжем отдельно)
                            if _is_ver:
                                continue
                            # Серия/часть/приложение — тоже не сливаем по embedding
                            continue
                        # Guard от слияния слов с общим корнем:
                        # «зашифрование» vs «расшифрование» — эмбеддинги близки,
                        # но это РАЗНЫЕ термины. Ключевой признак: почти одинаковые
                        # слова с РАЗНЫМ первым символом (за-/рас-, в-/на- и т.п.) —
                        # это разные приставочные слова. Алиасы при этом:
                        # регистр («Магма»/«Magma» — после lower первый символ
                        # совпадает), полное/сокращённое («ИСО/МЭК 18033-1» —
                        # первый символ тот же), пунктуация.
                        # НО: если одно норм-имя — ПОДСТРОКА другого («ПАО Сбербанк»
                        # ⊃ «Сбербанк», «ИСО/МЭК 18033-1» ⊃ «ИСО/МЭК 18033»),
                        # это полное/сокращённое → алиас, сливаем.
                        # ⚠️ ИСКЛЮЧЕНИЕ для document_ref: для стандартов/документов
                        # подстрока НЕ означает алиас! «ИСО/МЭК 18033» (серия) и
                        # «ИСО/МЭК 18033-1» (часть 1) — РАЗНЫЕ документы (по правилам
                        # ISO: -N = part, :YYYY = edition/версия). Сливать можно
                        # только версии: «18033-1:2005» и «18033-1:2015» — это
                        # редакции ОДНОГО документа. Проверка: у более длинного
                        # имени суффикс — год (4 цифры после ':' или '-').
                        _n1, _n2 = ei["norm"], ej["norm"]
                        _is_version_pair = False
                        if ei["type"] == "document_ref" and (_n1 in _n2 or _n2 in _n1):
                            _short, _long = (_n1, _n2) if _n1 in _n2 else (_n2, _n1)
                            _suffix = _long[len(_short):].strip()
                            # «18033-1:2005» vs «18033-1» → суффикс «:2005» / « 2005»
                            _is_version_pair = bool(re.fullmatch(r"[\s:–—-]*\d{4}", _suffix))
                        _is_substring = (
                            (len(_n1) >= 4 and _n1 in _n2) or
                            (len(_n2) >= 4 and _n2 in _n1)
                        ) and (ei["type"] != "document_ref" or _is_version_pair)
                        if not _is_substring:
                            if _n1 and _n2 and _n1[0] != _n2[0]:
                                import difflib
                                _ratio = difflib.SequenceMatcher(None, _n1, _n2).ratio()
                                if _ratio > 0.6:
                                    continue
                        sim = _cos(vecs[i], vecs[j])
                        # Два порога:
                        #  >= 0.95 — уверенное сходство, сливаем сразу (после guard'ов)
                        #  0.85-0.95 — «серая зона»: НЕ сливаем автоматически,
                        #  добавляем в кандидаты для LLM-верификации (см.
                        #  _verify_pairs_llm). Например «ЦБ» и «Банк России» —
                        #  эмбеддинги близки (оба про банк), но это алиас, а
                        #  «Сбербанк» и «Банк России» — РАЗНЫЕ организации.
                        if sim >= max(threshold, 0.95):
                            # Канонический — больший confidence
                            keeper = ei if (ei["confidence"], ei["doc_count"]) >= (ej["confidence"], ej["doc_count"]) else ej
                            dup = ej if keeper is ei else ei
                            # Проверяем, что dup ещё не запланирован
                            if dup["node_id"] not in self._resolution_plan and keeper["node_id"] != dup["node_id"]:
                                self._resolution_plan[dup["node_id"]] = keeper["node_id"]
                        elif sim >= 0.85 and len(ei["name"]) >= 4 and len(ej["name"]) >= 4:
                            # Серая зона → кандидат на LLM-верификацию
                            if ei["node_id"] not in self._resolution_plan and ej["node_id"] not in self._resolution_plan:
                                self._resolution_candidates.append({
                                    "a_id": ei["node_id"], "a_name": ei["name"], "a_type": ei["type"],
                                    "b_id": ej["node_id"], "b_name": ej["name"], "b_type": ej["type"],
                                    "sim": round(sim, 3),
                                })
                        elif sim < 0.85:
                            # Ниже порога — но может быть АББРЕВИАТУРА («ЦБ» = «Банк России»?
                            # нет — «ЦБ» = «Центральный банк»). Проверяем: одно имя —
                            # инициалы другого (все заглавные буквы короткого входят
                            # в первые буквы слов длинного), или подстрока.
                            _short_e, _long_e = (ei, ej) if len(ei["name"]) <= len(ej["name"]) else (ej, ei)
                            _sname, _lname = _short_e["name"], _long_e["name"]
                            if 2 <= len(_sname) <= 6 and _sname.isupper():
                                # Инициалы: «РСХБ» → Р,С,Х,Б
                                _initials = "".join(w[0] for w in re.split(r"\s+", _lname) if w)
                                if all(ch in _initials for ch in _sname):
                                    if _short_e["node_id"] not in self._resolution_plan and _long_e["node_id"] not in self._resolution_plan:
                                        self._resolution_candidates.append({
                                            "a_id": _long_e["node_id"], "a_name": _long_e["name"], "a_type": _long_e["type"],
                                            "b_id": _short_e["node_id"], "b_name": _short_e["name"], "b_type": _short_e["type"],
                                            "sim": round(sim, 3), "hint": "initials",
                                        })
        except Exception as e:
            logger.warning(f"[resolution] embed-шаг: {e}")

    async def _verify_pairs_llm(self, candidates: List[Dict], max_batch: int = 10):
        """LLM-верификация пар из «серой зоны» (0.85-0.95).

        Для каждой пары спрашиваем LLM: «это один и тот же объект?».
        Подтверждённые (same=true) добавляем в self._resolution_plan.

        Зачем: «ЦБ» и «Банк России» — эмбеддинги близки (оба про банк),
        но это алиас ОДНОЙ организации. «Сбербанк» и «Банк России» —
        тоже близкие эмбеддинги, но РАЗНЫЕ организации. Embedding не
        различает эти случаи — LLM различает (понимает контекст).

        Args:
            candidates: список пар из _resolution_candidates
            max_batch: сколько пар за один LLM-вызов (лимит токенов)
        """
        if not candidates:
            return
        try:
            cfg = self._get_graph_llm_config()
            if not cfg or not cfg.get("model"):
                logger.warning("[resolution] graph LLM не настроен — верификация пар пропущена")
                return

            model = cfg.get("model")
            llm_url = cfg.get("url", "")
            api_key = cfg.get("api_key", "")
            provider = cfg.get("provider", "ollama")

            import aiohttp
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            system_prompt = (
                "Ты — эксперт по разрешению сущностей (entity resolution). "
                "Определяешь, относятся ли два имени к одному и тому же реальному объекту "
                "(организация, документ, термин). Учитывай сокращения, аббревиатуры, "
                "полные и краткие названия. НЕ считай разные организации одним объектом "
                "только потому, что они из одной сферы. Отвечай строго JSON."
            )

            # Батчим пары
            for start in range(0, len(candidates), max_batch):
                batch = candidates[start:start + max_batch]
                pairs_desc = "\n".join([
                    f'{i + 1}. "{c["a_name"]}" ({c["a_type"]}) vs "{c["b_name"]}" ({c["b_type"]})'
                    for i, c in enumerate(batch)
                ])
                prompt = (
                    "Определи, являются ли следующие пары имён одним и тем же объектом.\n"
                    "Верни ТОЛЬКО JSON без markdown:\n"
                    '{"pairs": [{"a": "<имя1>", "b": "<имя2>", "same": true/false, "confidence": 0.0-1.0}]}\n\n'
                    "Пары:\n" + pairs_desc
                )

                # Температура из параметров привязки graph (админка), дефолт 0.0.
                # Ограничение 0.5: верификация пар требует стабильности.
                try:
                    _temp = float((cfg.get("parameters") or {}).get("temperature", 0.0))
                except (TypeError, ValueError):
                    _temp = 0.0
                _temp = min(max(_temp, 0.0), 0.5)
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": _temp,
                    # max_tokens достаточно: 10 пар × ~50 символов ≈ 500 символов
                    # ≈ 200-300 токенов. 800 хватало с запасом; больше не нужно.
                    "max_tokens": 1000,
                }
                # deepseek reasoning-модель: отключаем размышления.
                # no_think — параметр привязки функции graph (админка), по умолчанию True.
                _no_think = True
                try:
                    _no_think = bool((cfg.get("parameters") or {}).get("no_think", True))
                except Exception:
                    pass
                if provider in ("deepseek", "openai", "openrouter") and _no_think:
                    payload["thinking"] = {"type": "disabled"}

                if provider in ("openai", "deepseek", "openrouter"):
                    endpoint = f"{llm_url}/v1/chat/completions"
                else:
                    endpoint = f"{llm_url}/api/generate"
                    payload = {"model": model, "prompt": prompt, "stream": False,
                               "options": {"temperature": 0.0, "max_tokens": 400}}

                async with aiohttp.ClientSession() as session:
                    async with session.post(endpoint, json=payload, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=120)) as resp:
                        if resp.status != 200:
                            logger.warning(f"[resolution] LLM верификация HTTP {resp.status}")
                            continue
                        data = await resp.json()
                        if provider in ("openai", "deepseek", "openrouter"):
                            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        else:
                            content = data.get("response", "")

                # Парсим ответ
                try:
                    import json as _json
                    text_clean = content.strip()
                    if text_clean.startswith("```"):
                        text_clean = text_clean.lstrip("`").lstrip("json").strip()
                        text_clean = text_clean.rstrip("`").strip()
                    parsed = _json.loads(text_clean)
                    verdicts = parsed.get("pairs", [])
                except Exception as e:
                    logger.warning(f"[resolution] Не удалось распарсить LLM-ответ: {e}; raw={content[:200]}")
                    verdicts = []

                # Сопоставляем вердикты с кандидатами по именам
                verdict_map = {}
                for v in verdicts:
                    if isinstance(v, dict) and "a" in v and "b" in v:
                        verdict_map[(str(v.get("a", "")).strip().lower(), str(v.get("b", "")).strip().lower())] = v.get("same", False)
                        verdict_map[(str(v.get("b", "")).strip().lower(), str(v.get("a", "")).strip().lower())] = v.get("same", False)

                matched = 0
                confirmed = 0
                for c in batch:
                    key = (c["a_name"].strip().lower(), c["b_name"].strip().lower())
                    same = verdict_map.get(key)
                    if same is None:
                        continue
                    matched += 1
                    if same:
                        # Подтверждено LLM — сливаем (канонический по confidence)
                        a_id, b_id = c["a_id"], c["b_id"]
                        if a_id in self._resolution_plan or b_id in self._resolution_plan:
                            continue
                        # Выбираем keeper: больше source_docs/confidence — но здесь
                        # нет этих данных, используем порядок: a как keeper (первый)
                        self._resolution_plan[b_id] = a_id
                        confirmed += 1
                        logger.debug(f"[resolution] LLM подтвердил: {c['a_name']} = {c['b_name']}")
                logger.info(
                    f"[resolution] LLM-верификация батча: пар={len(batch)}, "
                    f"сопоставлено={matched}, подтверждено={confirmed}"
                )
                # Диагностика: первые пары батча (для отладки)
                if start == 0:
                    for _c in batch[:6]:
                        logger.info(
                            f"[resolution]   кандидат: {_c['a_name']} ({_c['a_type']}) "
                            f"~ {_c['b_name']} ({_c['b_type']}) sim={_c.get('sim')}"
                        )

        except Exception as e:
            import traceback
            logger.warning(f"[resolution] LLM-верификация: {e}\n{traceback.format_exc()}")

    def _get_graph_llm_config(self):
        """Получить конфигурацию LLM для графа (function_map:graph) из админки."""
        try:
            from src.api.services.provider_service import provider_service
            cfg = provider_service.get_function_llm_config("graph")
            if cfg and cfg.get("model"):
                return cfg
        except Exception:
            pass
        return None

    def load_alias_pairs(self, domain: str = "") -> List[Dict]:
        """Загрузить ПРИМЕНЯЕМЫЕ пары алиасов (reviewed + approved).

        Пары с source='pending' или verdict='rejected' НЕ применяются
        автоматически — их решает админ в админке (review_alias_pair).

        Args:
            domain: фильтр по предметной области (legal/medical/technical/...).
                Пустая строка = universal (общие пары) — применяются всегда.
        """
        try:
            from src.database.session import get_session_local
            from src.database.entity_alias_models import EntityAlias
            maker = get_session_local()
            session = maker()
            try:
                q = session.query(EntityAlias).filter(
                    EntityAlias.reviewed == True,  # noqa: E712
                    EntityAlias.verdict == "approved",
                )
                if domain:
                    # Пары домена + universal (общие применяются всегда)
                    q = q.filter(EntityAlias.domain.in_([domain, "universal"]))
                rows = q.all()
                return [r.to_dict() for r in rows]
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"[aliases] Не удалось загрузить entity_aliases: {e}")
            return []

    def apply_alias_pairs(self, domain: str = "") -> Dict[str, int]:
        """Применить известные пары алиасов к графу (детерминированно, без LLM).

        Для каждой пары (alias → canonical) из таблицы entity_aliases:
        - находим узел Entity с именем alias и типом
        - если canonical-узел не существует — создаём
        - переносим связи, source_docs, MENTIONS с alias на canonical
        - alias помечаем как aliases у canonical

        Args:
            domain: фильтр по предметной области. Пусто = только universal.
                Если None — применить ВСЕ approved (для админки «Применить всё»).

        Returns:
            {"applied": N, "created": M}
        """
        if not self.driver:
            return {"applied": 0, "created": 0}
        pairs = self.load_alias_pairs(domain=domain if domain is not None else "")
        if not pairs:
            return {"applied": 0, "created": 0}
        result = {"applied": 0, "created": 0}
        try:
            with self.driver.session() as session:
                for p in pairs:
                    canonical = (p.get("canonical_name") or "").strip()
                    alias = (p.get("alias") or "").strip()
                    etype = p.get("entity_type") or "organization"
                    if not canonical or not alias:
                        continue
                    try:
                        # Создаём canonical если нет
                        session.run(
                            """
                            MERGE (c:Entity {name: $canon, type: $type})
                            SET c.is_canonical = true
                            """,
                            canon=canonical, type=etype,
                        )
                        result["created"] += 1
                        # Сливаем alias в canonical
                        session.run(
                            """
                            MATCH (c:Entity {name: $canon, type: $type})
                            MATCH (a:Entity {name: $alias, type: $type})
                            WHERE id(a) <> id(c)
                            SET c.aliases = [x IN (coalesce(c.aliases, []) + [a.name]) WHERE NOT x IN coalesce(c.aliases, [])]
                            WITH c, a
                            OPTIONAL MATCH (ch:Chunk)-[r:MENTIONS]->(a)
                            FOREACH (_ IN CASE WHEN ch IS NOT NULL THEN [1] ELSE [] END |
                                MERGE (ch)-[:MENTIONS]->(c)
                            )
                            WITH c, a
                            SET c.source_docs = [x IN coalesce(c.source_docs, []) + coalesce(a.source_docs, []) | x]
                            WITH c, a
                            OPTIONAL MATCH (a)-[r2]->(t)
                            FOREACH (_ IN CASE WHEN t IS NOT NULL THEN [1] ELSE [] END |
                                MERGE (c)-[nr:RELATED_TO]->(t)
                                SET nr = properties(r2)
                            )
                            WITH c, a
                            OPTIONAL MATCH (s)-[r3]->(a)
                            FOREACH (_ IN CASE WHEN s IS NOT NULL THEN [1] ELSE [] END |
                                MERGE (s)-[nr2:RELATED_TO]->(c)
                                SET nr2 = properties(r3)
                            )
                            DETACH DELETE a
                            """,
                            canon=canonical, alias=alias, type=etype,
                        )
                        result["applied"] += 1
                    except Exception as e:
                        logger.warning(f"[aliases] Ошибка применения {alias}→{canonical}: {e}")
            if result["applied"]:
                logger.info(f"[aliases] Применено пар: {result['applied']}")
        except Exception as e:
            logger.warning(f"[aliases] apply_alias_pairs: {e}")
        return result

    def save_alias_pair(self, canonical: str, alias: str, entity_type: str = "organization",
                        source: str = "manual", comment: str = "", reviewed: bool = False,
                        verdict: str = "", domain: str = "universal") -> bool:
        """Сохранить пару алиасов в таблицу entity_aliases (для скриптов/админки)."""
        try:
            import uuid
            from src.database.session import get_session_local
            from src.database.entity_alias_models import EntityAlias
            maker = get_session_local()
            session = maker()
            try:
                existing = session.query(EntityAlias).filter_by(
                    canonical_name=canonical, alias=alias, entity_type=entity_type
                ).first()
                if existing:
                    return True  # уже есть
                row = EntityAlias(
                    id=str(uuid.uuid4()),
                    canonical_name=canonical,
                    alias=alias,
                    entity_type=entity_type,
                    domain=domain or "universal",
                    source=source,
                    comment=comment,
                    reviewed=reviewed,
                    verdict=verdict,
                )
                session.add(row)
                session.commit()
                return True
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"[aliases] save_alias_pair {alias}→{canonical}: {e}")
            return False

    def save_pending_pairs(self, candidates: List[Dict]) -> int:
        """Сохранить сомнительные пары (серая зона / LLM-кандидаты) на модерацию.

        Эти пары не применяются автоматически — админ решает в админке
        (подтвердить/отклонить). Источник: LLM-верификация и аббревиатуры,
        которые embedding не может разрешить однозначно.

        Args:
            candidates: список {a_name, b_name, a_type, b_type, sim, hint}

        Returns:
            Сколько новых пар добавлено в таблицу
        """
        added = 0
        for c in candidates:
            a_name = (c.get("a_name") or "").strip()
            b_name = (c.get("b_name") or "").strip()
            etype = c.get("a_type") or c.get("b_type") or "organization"
            if not a_name or not b_name or a_name == b_name:
                continue
            hint = c.get("hint", "")
            sim = c.get("sim")
            comment = f"авто: {hint}, sim={sim}" if hint or sim else "авто"
            # Пары уже подтверждённые LLM — тоже на модерацию (чтобы админ видел)
            if self.save_alias_pair(a_name, b_name, etype, source="pending",
                                    comment=comment, reviewed=False, verdict=""):
                added += 1
        return added

    def list_alias_pairs(self, include_pending: bool = False, verdict: str = "") -> List[Dict]:
        """Список пар алиасов из таблицы entity_aliases.

        Args:
            include_pending: включать непросмотренные (pending)
            verdict: фильтр по вердикту (approved/rejected/"" — без фильтра)

        Returns:
            Список словарей пар
        """
        try:
            from src.database.session import get_session_local
            from src.database.entity_alias_models import EntityAlias
            maker = get_session_local()
            session = maker()
            try:
                q = session.query(EntityAlias)
                if not include_pending:
                    q = q.filter(EntityAlias.reviewed == True)  # noqa: E712
                if verdict:
                    q = q.filter(EntityAlias.verdict == verdict)
                rows = q.order_by(EntityAlias.created_at.desc()).all()
                return [r.to_dict() for r in rows]
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"[aliases] list_alias_pairs: {e}")
            return []

    def delete_alias_pair(self, pair_id: str) -> bool:
        """Удалить пару алиасов по id."""
        try:
            from src.database.session import get_session_local
            from src.database.entity_alias_models import EntityAlias
            maker = get_session_local()
            session = maker()
            try:
                row = session.query(EntityAlias).filter_by(id=pair_id).first()
                if not row:
                    return False
                session.delete(row)
                session.commit()
                return True
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"[aliases] delete_alias_pair {pair_id}: {e}")
            return False

    def review_alias_pair(self, pair_id: str, verdict: str) -> bool:
        """Отметить пару как просмотренную с вердиктом (approved/rejected).

        approved — пара применяется apply_alias_pairs (детерминированно)
        rejected — пара отклонена админом, не применяется
        """
        if verdict not in ("approved", "rejected"):
            return False
        try:
            from src.database.session import get_session_local
            from src.database.entity_alias_models import EntityAlias
            maker = get_session_local()
            session = maker()
            try:
                row = session.query(EntityAlias).filter_by(id=pair_id).first()
                if not row:
                    return False
                row.reviewed = True
                row.verdict = verdict
                session.commit()
                return True
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"[aliases] review_alias_pair {pair_id}: {e}")
            return False

    def update_alias_pair(
        self, pair_id: str,
        alias: Optional[str] = None,
        canonical: Optional[str] = None,
        entity_type: Optional[str] = None,
        domain: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> bool:
        """Обновить содержимое пары алиасов (alias/canonical/type/domain/comment).

        reviewed/verdict НЕ трогаем: после правки пара остаётся в модерации,
        пока админ не подтвердит/отклонит. Возвращает False, если пара не найдена.
        """
        try:
            from src.database.session import get_session_local
            from src.database.entity_alias_models import EntityAlias
            maker = get_session_local()
            session = maker()
            try:
                row = session.query(EntityAlias).filter_by(id=pair_id).first()
                if not row:
                    return False
                if alias is not None and alias.strip():
                    row.alias = alias.strip()
                if canonical is not None and canonical.strip():
                    row.canonical_name = canonical.strip()
                if entity_type:
                    row.entity_type = entity_type
                if domain:
                    row.domain = domain
                if comment is not None:
                    row.comment = comment
                session.commit()
                return True
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"[aliases] update_alias_pair {pair_id}: {e}")
            return False

    # ============================================================
    # Валидация качества сущностей (Neo4j Best Practice)
    # ============================================================

    def validate_entities(self, document_id: str) -> Dict[str, Any]:
        """Проверить качество извлечённых сущностей документа.
        
        Критерии:
        - Короткие имена (<3 символов) — подозрительно
        - Слишком длинные имена (>200 символов) — вероятно, не сущность
        - Нет связей MENTIONS — осиротевшая сущность
        - Низкий confidence (<0.5) — неуверенное извлечение
        """
        if not self.driver:
            return {"valid": True, "warnings": [], "error": "Neo4j не подключён"}
        try:
            with self.driver.session() as session:
                result = session.run("""
                    MATCH (d:Document {id: $doc_id})-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e:Entity)
                    OPTIONAL MATCH (e)-[r]->()
                    OPTIONAL MATCH ()-[r2]->(e)
                    WITH e, count(r) + count(r2) as rel_count
                    RETURN 
                        count(e) as total,
                        sum(CASE WHEN size(e.name) < 3 THEN 1 ELSE 0 END) as too_short,
                        sum(CASE WHEN size(e.name) > 200 THEN 1 ELSE 0 END) as too_long,
                        sum(CASE WHEN e.confidence < 0.5 THEN 1 ELSE 0 END) as low_confidence,
                        sum(CASE WHEN rel_count = 0 THEN 1 ELSE 0 END) as orphaned
                """, doc_id=document_id)
                rec = result.single()
                if rec:
                    warnings = []
                    if rec["too_short"] > 0:
                        warnings.append(f"{rec['too_short']} сущностей с именем <3 символов")
                    if rec["too_long"] > 0:
                        warnings.append(f"{rec['too_long']} сущностей с именем >200 символов")
                    if rec["low_confidence"] > 0:
                        warnings.append(f"{rec['low_confidence']} сущностей с confidence <0.5")
                    if rec["orphaned"] > 0:
                        warnings.append(f"{rec['orphaned']} сущностей без связей")
                    return {
                        "valid": len(warnings) == 0,
                        "total": rec["total"],
                        "warnings": warnings,
                        "quality_score": max(0, 100 - len(warnings) * 15)
                    }
                return {"valid": True, "warnings": [], "total": 0, "quality_score": 100}
        except Exception as e:
            return {"valid": False, "warnings": [str(e)], "error": str(e)}

    # ============================================================
    # Поиск и навигация по графу
    # ============================================================

    def search_entities(self, query: str, entity_type: str = None, limit: int = 10) -> List[Dict]:
        """Поиск сущностей по имени (CONTAINS).
        
        Использует string interpolation для CONTAINS — надёжнее параметризации в Neo4j.
        """
        if not self.driver:
            return []
        try:
            with self.driver.session() as session:
                safe_query = query.replace('"', '\\"').replace("'", "\\'")
                if entity_type:
                    safe_type = entity_type.replace('"', '\\"').replace("'", "\\'")
                    cypher = f"""
                        MATCH (e:Entity)
                        WHERE toLower(e.name) CONTAINS toLower("{safe_query}") AND e.type = "{safe_type}"
                        RETURN e.name as name, e.type as type, e.confidence as confidence,
                        size(coalesce(e.source_docs, [])) as doc_count
                        ORDER BY e.confidence DESC
                        LIMIT {limit}
                    """
                else:
                    cypher = f"""
                        MATCH (e:Entity)
                        WHERE toLower(e.name) CONTAINS toLower("{safe_query}")
                        RETURN e.name as name, e.type as type, e.confidence as confidence,
                        size(coalesce(e.source_docs, [])) as doc_count
                        ORDER BY e.confidence DESC
                        LIMIT {limit}
                    """
                result = session.run(cypher)
                return [dict(r) for r in result]
        except Exception as e:
            logger.warning(f"Ошибка поиска сущностей: {e}")
            return []

    def search_entities_fts(self, query: str, limit: int = 10) -> List[Dict]:
        """Полнотекстовый поиск сущностей (Neo4j text index).
        
        Быстрее чем CONTAINS для больших графов.
        Требует TEXT INDEX на Entity.name.
        """
        if not self.driver:
            return []
        try:
            with self.driver.session() as session:
                # Lucene синтаксис: * для частичного совпадения
                fts_query = f'"{query}"~ OR {query}*'
                result = session.run("""
                    CALL db.index.fulltext.queryNodes("txt_entity_name", $query)
                    YIELD node, score
                    RETURN node.name as name, node.type as type, 
                           node.confidence as confidence, score
                    ORDER BY score DESC
                    LIMIT $limit
                """, query=fts_query, limit=limit)
                return [dict(r) for r in result]
        except Exception:
            # Fallback к CONTAINS если FTS индекс не создан
            return self.search_entities(query, limit=limit)

    def get_document_entities(self, document_id: str) -> List[Dict]:
        """Получить все сущности документа с группировкой по типу."""
        if not self.driver:
            return []
        try:
            with self.driver.session() as session:
                result = session.run("""
                    MATCH (d:Document {id: $doc_id})-[:HAS_CHUNK]->(c:Chunk)-[:MENTIONS]->(e:Entity)
                    RETURN e.name as name, e.type as type, e.confidence as confidence,
                           c.id as chunk_id, c.chunk_seq as chunk_seq,
                           size(coalesce(e.source_docs, [])) as doc_count
                    ORDER BY c.chunk_seq, e.type
                """, doc_id=document_id)
                return [dict(r) for r in result]
        except Exception as e:
            logger.warning(f"Ошибка получения сущностей документа: {e}")
            return []

    def get_entity_graph(self, entity_name: str, depth: int = 2) -> List[Dict]:
        """Получить подграф вокруг сущности — multi-hop обход.
        
        Используется для GraphRAG: понимание контекста сущности через её соседей.
        """
        if not self.driver:
            return []
        try:
            with self.driver.session() as session:
                # ВАЖНО: Neo4j запрещает параметр в переменной длине пути
                # ([*1..$depth] → «Parameter maps cannot be used in MATCH patterns»).
                # Поэтому depth подставляем литералом, предварительно валидируя int.
                try:
                    depth_i = int(depth) if depth else 2
                except (TypeError, ValueError):
                    depth_i = 2
                depth_i = max(1, min(depth_i, 4))
                query = (
                    "MATCH path = (e:Entity {name: $name})"
                    f"-[*1..{depth_i}]-(related) RETURN path LIMIT 50"
                )
                result = session.run(query, name=entity_name)
                nodes = set()
                edges = []
                for record in result:
                    path = record["path"]
                    for node in path.nodes:
                        nodes.add((
                            node.get("name", node.get("id", "")),
                            list(node.labels)[0] if node.labels else "Unknown"
                        ))
                    for rel in path.relationships:
                        edges.append({
                            "source": rel.start_node.get("name", ""),
                            "target": rel.end_node.get("name", ""),
                            "type": rel.type
                        })
                return [{"nodes": [{"name": n, "type": t} for n, t in nodes], "edges": edges}]
        except Exception as e:
            logger.warning(f"Ошибка получения графа: {e}")
            return []

    def hybrid_search(self, query_entities: List[str], doc_ids: List[str] = None) -> List[str]:
        """Гибридный поиск: найти чанки, связанные с заданными сущностями.
        
        Чем больше искомых сущностей в чанке — тем выше ранк.
        Использует CONTAINS для частичного совпадения (найдёт «криптография» даже если сущность называется «ГОСТ Р криптозащита»).
        """
        if not self.driver or not query_entities:
            return []
        try:
            # Строим WHERE с CONTAINS для каждого термина
            where_clauses = []
            params = {}
            for i, term in enumerate(query_entities):
                param_name = f"term_{i}"
                where_clauses.append(f"e.name CONTAINS ${param_name}")
                params[param_name] = term
            
            where_str = " OR ".join(where_clauses)
            query = f"""
                MATCH (e:Entity)<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
                WHERE {where_str}
                RETURN DISTINCT c.id as chunk_id, c.text_preview as text,
                       d.id as doc_id, d.filename as filename,
                       count(e) as entity_count
                ORDER BY entity_count DESC
                LIMIT 20
            """
            
            with self.driver.session() as session:
                if doc_ids:
                    params["doc_ids"] = doc_ids
                    query = f"""
                        MATCH (e:Entity)<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
                        WHERE ({where_str}) AND d.id IN $doc_ids
                        RETURN DISTINCT c.id as chunk_id, c.text_preview as text,
                               d.id as doc_id, d.filename as filename,
                               count(e) as entity_count
                        ORDER BY entity_count DESC
                        LIMIT 20
                    """
                result = session.run(query, **params)
                return [dict(r) for r in result]
        except Exception as e:
            logger.warning(f"Ошибка гибридного поиска: {e}")
            return []

    # ============================================================
    # Управление графом
    # ============================================================

    def execute_cypher(self, query: str, limit: int = 100) -> List[Dict]:
        """Выполнить произвольный Cypher-запрос (только чтение).
        
        Защита от модификации данных через UI.
        """
        if not self.driver:
            return []
        try:
            query_upper = query.upper().strip()
            forbidden = ["CREATE ", "MERGE ", "SET ", "DELETE ", "REMOVE ", "DROP ", "CALL "]
            for f in forbidden:
                if f in query_upper:
                    raise ValueError(f"Запрещённая операция: {f.strip()}. Разрешены только MATCH, RETURN.")
            
            with self.driver.session() as session:
                result = session.run(query)
                records = []
                for idx, record in enumerate(result):
                    if idx >= limit:
                        break
                    records.append(dict(record))
                return records
        except Exception as e:
            logger.error(f"Ошибка Cypher-запроса: {e}")
            raise e

    def get_stats(self) -> Dict[str, int]:
        """Статистика графа — Lexical + Domain."""
        if not self.driver:
            return {"documents": 0, "chunks": 0, "entities": 0, "relations": 0, "cross_doc_entities": 0}
        try:
            with self.driver.session() as session:
                docs = session.run("MATCH (d:Document) RETURN count(d) as c").single()["c"]
                chunks = session.run("MATCH (c:Chunk) RETURN count(c) as c").single()["c"]
                entities = session.run("MATCH (e:Entity) RETURN count(e) as c").single()["c"]
                rels = session.run("MATCH ()-[r]->() RETURN count(r) as c").single()["c"]
                # Сущности, встречающиеся в >1 документе — показатель качества графа
                cross = session.run(
                    "MATCH (e:Entity) WHERE size(coalesce(e.source_docs, [])) > 1 RETURN count(e) as c"
                ).single()["c"]
                return {
                    "documents": docs, "chunks": chunks,
                    "entities": entities, "relations": rels,
                    "cross_doc_entities": cross
                }
        except Exception:
            return {"documents": 0, "chunks": 0, "entities": 0, "relations": 0, "cross_doc_entities": 0}

    def clear_document(self, document_id: str):
        """Удалить документ и его чанки из Lexical Graph.
        
        Сущности Domain Graph удаляются только если осиротели
        (нет MENTIONS ни от одного чанка).
        """
        if not self.driver:
            return
        try:
            with self.driver.session() as session:
                # 1. Удалить документ и чанки
                session.run("""
                    MATCH (d:Document {id: $doc_id})
                    OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
                    DETACH DELETE d, c
                """, doc_id=document_id)
                
                # 2. Удалить doc_id из source_docs сущностей
                session.run("""
                    MATCH (e:Entity)
                    WHERE $doc_id IN coalesce(e.source_docs, [])
                    SET e.source_docs = [x IN e.source_docs WHERE x <> $doc_id]
                """, doc_id=document_id)
                
                # 3. Удалить осиротевшие сущности
                session.run("""
                    MATCH (e:Entity)
                    WHERE NOT (()-[:MENTIONS]->(e))
                    DETACH DELETE e
                """)
        except Exception as e:
            logger.warning(f"Ошибка удаления документа из графа: {e}")

    def set_domain_schema(self, schema: Dict[str, Dict[str, str]]):
        """Установить доменную схему сущностей.
        
        Args:
            schema: {"person": {"label": "Человек", "color": "#7170ff"}, ...}
        """
        self._domain_schema = dict(schema)
        logger.info(f"Доменная схема обновлена: {list(schema.keys())}")

    def get_domain_schema(self) -> Dict[str, Dict[str, str]]:
        """Получить текущую доменную схему."""
        return dict(self._domain_schema)


# Глобальный экземпляр
kg_service = KnowledgeGraphService()
