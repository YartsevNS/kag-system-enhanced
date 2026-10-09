"""Отладка: почему у переобработанного документа нет версий/фрагментов у фактов."""
import os

from neo4j import GraphDatabase

DOC = os.environ.get("DOC_ID", "")  # передаётся аргументом окружения
uri = os.environ.get("NEO4J_URI") or f"bolt://{os.environ.get('NEO4J_HOST', 'neo4j')}:{os.environ.get('NEO4J_PORT', 7687)}"
drv = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))

with drv.session() as s:
    print("документ:", DOC[:12])
    q = s.run("MATCH (doc:Document)-[:HAS_CHUNK]->(c:Chunk)-[:MENTIONS]->(e:Entity) "
              "WHERE doc.id = $doc RETURN e LIMIT 2", doc=DOC)
    n = 0
    for rec in q:
        e = rec["e"]
        n += 1
        print("сущность:", e.get("name"), "| тип:", e.get("type"))
        print("   ключи:", sorted(e.keys()))
    print("сущностей найдено:", n)

    q2 = s.run("MATCH (doc:Document)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e1:Entity)-[rel]->(e2:Entity) "
               "WHERE doc.id = $doc RETURN type(rel) AS t, keys(rel) AS k LIMIT 3", doc=DOC)
    for rec in q2:
        print("связь:", rec["t"], "| ключи:", rec["k"])

    # сколько связей вообще с новыми свойствами
    for prop in ("chunk_id", "schema_version", "doc_id"):
        c = s.run(f"MATCH ()-[r]->() WHERE r.{prop} IS NOT NULL RETURN count(r) AS n").single()["n"]
        print(f"связей с {prop}: {c}")
drv.close()
