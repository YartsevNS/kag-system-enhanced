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

    def __init__(self, uri: str = "bolt://neo4j:7687", user: str = "neo4j", password: str = "kagneo4j2026"):
        self._uri = uri
        self._user = user
        self._password = password
        self._driver = None
        self._initialized = False
        self._domain_schema = dict(self.DEFAULT_DOMAIN_SCHEMA)

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
                # Composite constraint: (name, type) уникальность = автоматический dedup
                try:
                    session.run(
                        "CREATE CONSTRAINT cst_entity_unique IF NOT EXISTS "
                        "FOR (e:Entity) REQUIRE (e.name, e.type) IS NODE KEY"
                    )
                except Exception:
                    pass  # Neo4j Community может не поддерживать NODE KEY
                
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
                        WHERE e.name CONTAINS "{safe_query}" AND e.type = "{safe_type}"
                        RETURN e.name as name, e.type as type, e.confidence as confidence,
                               size(coalesce(e.source_docs, [])) as doc_count
                        ORDER BY e.confidence DESC
                        LIMIT {limit}
                    """
                else:
                    cypher = f"""
                        MATCH (e:Entity)
                        WHERE e.name CONTAINS "{safe_query}"
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
                result = session.run("""
                    MATCH path = (e:Entity {name: $name})-[*1..$depth]-(related)
                    RETURN path LIMIT 50
                """, name=entity_name, depth=depth)
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
        """
        if not self.driver or not query_entities:
            return []
        try:
            with self.driver.session() as session:
                if doc_ids:
                    result = session.run("""
                        MATCH (e:Entity)<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
                        WHERE e.name IN $entities AND d.id IN $doc_ids
                        RETURN DISTINCT c.id as chunk_id, c.text_preview as text,
                               d.id as doc_id, d.filename as filename,
                               count(e) as entity_count
                        ORDER BY entity_count DESC
                        LIMIT 20
                    """, entities=query_entities, doc_ids=doc_ids)
                else:
                    result = session.run("""
                        MATCH (e:Entity)<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
                        WHERE e.name IN $entities
                        RETURN DISTINCT c.id as chunk_id, c.text_preview as text,
                               d.id as doc_id, d.filename as filename,
                               count(e) as entity_count
                        ORDER BY entity_count DESC
                        LIMIT 20
                    """, entities=query_entities)
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
