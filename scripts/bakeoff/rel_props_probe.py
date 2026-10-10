"""Проверка: проставляются ли свойства на УЖЕ существующей связи (не новой).

Прошлый опыт показал: на новой связи свойства ставятся, а у связей документа их нет. Проверяем
именно случай «ребро уже есть» — конвейер при переобработке MERGE'ит те же пары.

Запуск: docker exec kag-system_worker_1 python /app/data/rel_props_probe.py
"""
import os

from neo4j import GraphDatabase

from src.indexing.knowledge_graph import Relation, kg_service

uri = os.environ.get("NEO4J_URI") or "bolt://neo4j:7687"
drv = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))

with drv.session() as s:
    row = s.run("""
        MATCH (a:Entity)-[r]->(b:Entity)
        WHERE NOT type(r) IN ['MENTIONS','HAS_CHUNK','HAS_SECTION','SECTION_CHUNK']
          AND r.schema_version IS NULL
        RETURN a.name AS src, type(r) AS t, b.name AS dst, keys(r) AS k
        LIMIT 3
    """)
    targets = [dict(r) for r in row]

print("связи без версии (первые):")
for t in targets:
    print(f"   {t['src'][:26]} -[{t['t']}]-> {t['dst'][:26]} | ключи: {t['k']}")

if not targets:
    print("нечего проверять")
    raise SystemExit(0)

t0 = targets[0]
n = kg_service.batch_create_relations([
    Relation(source=t0["src"], target=t0["dst"], type=t0["t"],
             document_id="probe-doc", chunk_id="probe-chunk")
])
print(f"\nповторная запись той же связи: записано {n}")

with drv.session() as s:
    r2 = s.run("MATCH (a:Entity {name: $src})-[r]->(b:Entity {name: $dst}) WHERE type(r) = $t "
               "RETURN keys(r) AS k, r.chunk_id AS c, r.schema_version AS v",
               src=t0["src"], dst=t0["dst"], t=t0["t"]).single()
    print("после повторной записи: ключи:", r2["k"], "| фрагмент:", r2["c"], "| версия:", r2["v"])
    nover = s.run("""
        MATCH ()-[r]->()
        WHERE NOT type(r) IN ['MENTIONS','HAS_CHUNK','HAS_SECTION','SECTION_CHUNK']
        RETURN count(r) AS vsego,
               count(CASE WHEN r.schema_version IS NOT NULL THEN 1 END) AS s_versiey
    """).single()
    print(f"\nвсего «содержательных» связей: {nover['vsego']}, с версией: {nover['s_versiey']}")
drv.close()
