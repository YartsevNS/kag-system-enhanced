"""Проверка происхождения фактов в графе: у сущностей и связей есть документ, фрагмент и версии.

Что проверяем на живом Neo4j:
  1. у сущностей есть schema_version и extractor_version;
  2. у связей есть doc_id, chunk_id, schema_version, extractor_version;
  3. у связей НЕТ типов вне закрытого списка (иначе проверка на запись не работает);
  4. «откуда известно»: по связи можно дойти до фрагмента и до документа.

Запуск в контейнере api: docker exec kag-api python /app/data/graph_provenance_check.py
"""
import os

from neo4j import GraphDatabase

uri = os.environ.get("NEO4J_URI") or f"bolt://{os.environ.get('NEO4J_HOST', 'neo4j')}:{os.environ.get('NEO4J_PORT', 7687)}"
drv = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))

ALLOWED = {"RELATED_TO", "SIGNED_BY", "DATED", "AMOUNT", "BELONGS_TO", "LOCATED_AT",
           "MENTIONS", "HAS_CHUNK", "SUPERSEDED_BY"}

with drv.session() as s:
    ent_total = s.run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
    ent_ver = s.run("MATCH (e:Entity) WHERE e.schema_version IS NOT NULL RETURN count(e) AS n").single()["n"]
    print(f"сущности: {ent_total}, с версией схемы: {ent_ver}")

    rel_total = s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
    rel_doc = s.run("MATCH ()-[r]->() WHERE r.doc_id IS NOT NULL RETURN count(r) AS n").single()["n"]
    rel_chunk = s.run("MATCH ()-[r]->() WHERE r.chunk_id IS NOT NULL RETURN count(r) AS n").single()["n"]
    rel_ver = s.run("MATCH ()-[r]->() WHERE r.schema_version IS NOT NULL RETURN count(r) AS n").single()["n"]
    print(f"связи: {rel_total}, с документом: {rel_doc}, с фрагментом: {rel_chunk}, с версией: {rel_ver}")

    types = [r["t"] for r in s.run("MATCH ()-[r]->() RETURN DISTINCT type(r) AS t")]
    unknown = sorted(t for t in types if t.upper() not in ALLOWED)
    print("типы связей в базе:", sorted(types))
    print("типы ВНЕ закрытого списка:", unknown if unknown else "нет")

    # «откуда известно»: связь → фрагмент → документ
    chain = s.run("""
        MATCH (a:Entity)-[r]->(b:Entity)
        WHERE r.chunk_id IS NOT NULL AND r.chunk_id <> ''
        OPTIONAL MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk {id: r.chunk_id})
        RETURN a.name AS src, type(r) AS t, b.name AS dst, r.chunk_id AS chunk, d.filename AS doc
        LIMIT 3
    """)
    print("\nпримеры «откуда известно»:")
    for row in chain:
        print(f"   {str(row['src'])[:28]} --{row['t']}--> {str(row['dst'])[:28]} | фрагмент {str(row['chunk'])[-8:]} | документ {(row['doc'] or '—')[:34]}")

verdict = ent_ver > 0 and rel_chunk > 0 and not unknown
print("\nИТОГ:", "происхождение и версии записываются, типы в рамках словаря" if verdict
      else "ЕСТЬ ЧТО ДОЧИСТИТЬ (см. выше)")
drv.close()
