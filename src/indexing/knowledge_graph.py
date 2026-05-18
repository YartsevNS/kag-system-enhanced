"""
Knowledge Graph Service — Neo4j-сервис для KAG.

Хранит граф знаний: документы, чанки, сущности и связи между ними.
Использует Neo4j для:
- Хранения сущностей (люди, организации, даты, суммы, места)
- Связей между чанками и сущностями
- Multi-hop рассуждений (графовые запросы)
- Гибридного поиска (граф + вектор)

API: Bolt-драйвер neo4j (pip install neo4j)
"""

from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from loguru import logger
import json


@dataclass
class Entity:
    """Сущность, извлечённая из документа."""
    name: str
    type: str  # person, organization, date, money, location, document_ref, legal_term
    chunk_id: str = ""
    document_id: str = ""
    confidence: float = 1.0
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass  
class Relation:
    """Связь между сущностями."""
    source: str
    target: str
    type: str  # MENTIONS, BELONGS_TO, SIGNED_BY, DATED, AMOUNT, RELATED_TO
    document_id: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)


class KnowledgeGraphService:
    """Сервис графа знаний на Neo4j."""

    def __init__(self, uri: str = "bolt://neo4j:7687", user: str = "neo4j", password: str = "kagneo4j2026"):
        self._uri = uri
        self._user = user
        self._password = password
        self._driver = None
        self._initialized = False

    @property
    def driver(self):
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
        """Создать индексы и constraints."""
        if not self.driver or self._initialized:
            return
        try:
            with self.driver.session() as session:
                # Индексы
                session.run("CREATE INDEX doc_id IF NOT EXISTS FOR (d:Document) ON (d.id)")
                session.run("CREATE INDEX chunk_id IF NOT EXISTS FOR (c:Chunk) ON (c.id)")
                session.run("CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)")
                session.run("CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)")
                # Constraints
                try:
                    session.run("CREATE CONSTRAINT doc_unique IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE")
                    session.run("CREATE CONSTRAINT chunk_unique IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE")
                except Exception:
                    pass  # Уже существуют
                logger.info("Neo4j схема инициализирована")
                self._initialized = True
        except Exception as e:
            logger.warning(f"Ошибка инициализации схемы Neo4j: {e}")

    def create_document_node(self, document_id: str, filename: str, metadata: Dict = None):
        """Создать узел документа."""
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
                    metadata=metadata or {}
                )
        except Exception as e:
            logger.warning(f"Ошибка создания узла документа: {e}")

    def create_chunk_node(self, chunk_id: str, document_id: str, text: str, chunk_seq: int = 0):
        """Создать узел чанка и связь с документом."""
        if not self.driver:
            return
        try:
            with self.driver.session() as session:
                session.run(
                    """
                    MERGE (c:Chunk {id: $chunk_id})
                    SET c.text = $text,
                        c.chunk_seq = $chunk_seq,
                        c.updated_at = datetime()
                    WITH c
                    MATCH (d:Document {id: $doc_id})
                    MERGE (d)-[:HAS_CHUNK]->(c)
                    """,
                    chunk_id=chunk_id,
                    doc_id=document_id,
                    text=text[:500],  # Храним первые 500 символов в графе
                    chunk_seq=chunk_seq
                )
        except Exception as e:
            logger.warning(f"Ошибка создания узла чанка: {e}")

    def create_entity(self, entity: Entity):
        """Создать узел сущности и связь с чанком."""
        if not self.driver:
            return
        try:
            with self.driver.session() as session:
                session.run(
                    """
                    MERGE (e:Entity {name: $name, type: $type})
                    SET e.confidence = $confidence,
                        e.properties = $properties,
                        e.updated_at = datetime()
                    WITH e
                    MATCH (c:Chunk {id: $chunk_id})
                    MERGE (c)-[:MENTIONS]->(e)
                    """,
                    name=entity.name,
                    type=entity.type,
                    confidence=entity.confidence,
                    properties=json.dumps(entity.properties, ensure_ascii=False) if entity.properties else "{}",
                    chunk_id=entity.chunk_id
                )
        except Exception as e:
            logger.warning(f"Ошибка создания сущности: {e}")

    def create_relation(self, rel: Relation):
        """Создать связь между сущностями."""
        if not self.driver:
            return
        try:
            with self.driver.session() as session:
                session.run(
                    f"""
                    MATCH (a:Entity {{name: $source}})
                    MATCH (b:Entity {{name: $target}})
                    MERGE (a)-[:{rel.type}]->(b)
                    """,
                    source=rel.source,
                    target=rel.target
                )
        except Exception as e:
            logger.warning(f"Ошибка создания связи: {e}")

    def search_entities(self, query: str, entity_type: str = None, limit: int = 10) -> List[Dict]:
        """Поиск сущностей по имени."""
        if not self.driver:
            return []
        try:
            with self.driver.session() as session:
                if entity_type:
                    result = session.run(
                        """
                        MATCH (e:Entity)
                        WHERE e.name CONTAINS $query AND e.type = $type
                        RETURN e.name as name, e.type as type, e.confidence as confidence
                        ORDER BY e.confidence DESC
                        LIMIT $limit
                        """,
                        query=query, type=entity_type, limit=limit
                    )
                else:
                    result = session.run(
                        """
                        MATCH (e:Entity)
                        WHERE e.name CONTAINS $query
                        RETURN e.name as name, e.type as type, e.confidence as confidence
                        ORDER BY e.confidence DESC
                        LIMIT $limit
                        """,
                        query=query, limit=limit
                    )
                return [dict(r) for r in result]
        except Exception as e:
            logger.warning(f"Ошибка поиска сущностей: {e}")
            return []

    def get_document_entities(self, document_id: str) -> List[Dict]:
        """Получить все сущности документа."""
        if not self.driver:
            return []
        try:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (d:Document {id: $doc_id})-[:HAS_CHUNK]->(c:Chunk)-[:MENTIONS]->(e:Entity)
                    RETURN e.name as name, e.type as type, e.confidence as confidence,
                           c.id as chunk_id, c.chunk_seq as chunk_seq
                    ORDER BY c.chunk_seq, e.type
                    """,
                    doc_id=document_id
                )
                return [dict(r) for r in result]
        except Exception as e:
            logger.warning(f"Ошибка получения сущностей документа: {e}")
            return []

    def get_entity_graph(self, entity_name: str, depth: int = 2) -> List[Dict]:
        """Получить подграф вокруг сущности."""
        if not self.driver:
            return []
        try:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH path = (e:Entity {name: $name})-[*1..$depth]-(related)
                    RETURN path LIMIT 50
                    """,
                    name=entity_name,
                    depth=depth
                )
                nodes = set()
                edges = []
                for record in result:
                    path = record["path"]
                    for node in path.nodes:
                        nodes.add((node.get("name", node.get("id", "")), 
                                   list(node.labels)[0] if node.labels else "Unknown"))
                    for rel in path.relationships:
                        edges.append({
                            "source": rel.start_node.get("name", rel.start_node.get("id", "")),
                            "target": rel.end_node.get("name", rel.end_node.get("id", "")),
                            "type": rel.type
                        })
                return [{"nodes": [{"name": n, "type": t} for n, t in nodes], "edges": edges}]
        except Exception as e:
            logger.warning(f"Ошибка получения графа: {e}")
            return []

    def hybrid_search(self, query_entities: List[str], doc_ids: List[str] = None) -> List[str]:
        """Гибридный поиск: найти чанки, связанные с заданными сущностями."""
        if not self.driver or not query_entities:
            return []
        try:
            with self.driver.session() as session:
                if doc_ids:
                    result = session.run(
                        """
                        MATCH (e:Entity)<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
                        WHERE e.name IN $entities AND d.id IN $doc_ids
                        RETURN DISTINCT c.id as chunk_id, c.text as text, 
                               d.id as doc_id, d.filename as filename,
                               count(e) as entity_count
                        ORDER BY entity_count DESC
                        LIMIT 20
                        """,
                        entities=query_entities,
                        doc_ids=doc_ids
                    )
                else:
                    result = session.run(
                        """
                        MATCH (e:Entity)<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
                        WHERE e.name IN $entities
                        RETURN DISTINCT c.id as chunk_id, c.text as text, 
                               d.id as doc_id, d.filename as filename,
                               count(e) as entity_count
                        ORDER BY entity_count DESC
                        LIMIT 20
                        """,
                        entities=query_entities
                    )
                return [dict(r) for r in result]
        except Exception as e:
            logger.warning(f"Ошибка гибридного поиска: {e}")
            return []

    def execute_cypher(self, query: str, limit: int = 100) -> List[Dict]:
        """Выполнить произвольный Cypher-запрос (только чтение)."""
        if not self.driver:
            return []
        try:
            # Базовая защита от модификации данных через UI
            query_upper = query.upper()
            if any(forbidden in query_upper for forbidden in ["CREATE", "MERGE", "SET", "DELETE", "REMOVE", "DROP", "CALL "]):
                raise ValueError("Разрешены только запросы на чтение (MATCH, RETURN)")
            
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
        """Статистика графа."""
        if not self.driver:
            return {"documents": 0, "chunks": 0, "entities": 0, "relations": 0}
        try:
            with self.driver.session() as session:
                docs = session.run("MATCH (d:Document) RETURN count(d) as c").single()["c"]
                chunks = session.run("MATCH (c:Chunk) RETURN count(c) as c").single()["c"]
                entities = session.run("MATCH (e:Entity) RETURN count(e) as c").single()["c"]
                rels = session.run("MATCH ()-[r]->() RETURN count(r) as c").single()["c"]
                return {"documents": docs, "chunks": chunks, "entities": entities, "relations": rels}
        except Exception:
            return {"documents": 0, "chunks": 0, "entities": 0, "relations": 0}

    def clear_document(self, document_id: str):
        """Удалить документ и все связанные узлы из графа."""
        if not self.driver:
            return
        try:
            with self.driver.session() as session:
                session.run(
                    """
                    MATCH (d:Document {id: $doc_id})
                    OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
                    OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)
                    DETACH DELETE d, c, e
                    """,
                    doc_id=document_id
                )
        except Exception as e:
            logger.warning(f"Ошибка удаления документа из графа: {e}")


# Глобальный экземпляр
kg_service = KnowledgeGraphService()
