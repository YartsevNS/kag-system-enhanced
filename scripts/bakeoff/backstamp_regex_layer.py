"""Дописать происхождение связям слоя правил, записанным до введения версий.

Зачем. Замер показал расхождение двух путей записи: у связей модели версия схемы есть (1892), у связей
правил — нет (682). «Пусто» неотличимо от «забыли проставить», а объединение путей без этого шага
оставило бы дыру. Правильное действие — не выдумать версию, а честно пометить: эти связи сделаны
СЛОЕМ ПРАВИЛ до введения версий (`regex-legacy`), слой у них уже проставлен.

Сам код каркаса исправлен: версия схемы берётся из ЕДИНОГО источника
(`knowledge_graph.SCHEMA_VERSION`), а не из своей константы.

Запуск в контейнере api:
  docker exec kag-api python /app/data/backstamp_regex_layer.py --dry-run
  docker exec kag-api python /app/data/backstamp_regex_layer.py
"""
import os
import sys

from neo4j import GraphDatabase

STAMP = "regex-legacy"
uri = os.environ.get("NEO4J_URI") or f"bolt://{os.environ.get('NEO4J_HOST', 'neo4j')}:{os.environ.get('NEO4J_PORT', 7687)}"
drv = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))
dry = "--dry-run" in sys.argv

SCOPE = "NOT type(r) IN ['MENTIONS','HAS_CHUNK','HAS_SECTION','SECTION_CHUNK']"

with drv.session() as s:
    before = s.run(f"""
        MATCH ()-[r]->() WHERE {SCOPE}
        RETURN coalesce(r.layer,'(нет)') AS layer,
               count(r) AS vsego,
               count(CASE WHEN r.schema_version IS NULL THEN 1 END) AS bez_versii
        ORDER BY vsego DESC
    """)
    print("до правки:")
    rows = [dict(r) for r in before]
    for r in rows:
        print(f"   слой {r['layer']:<14} всего {r['vsego']:<6} без версии {r['bez_versii']}")

    if dry:
        n = s.run(f"MATCH ()-[r]->() WHERE {SCOPE} AND r.layer = 'regex' AND r.schema_version IS NULL "
                  "RETURN count(r) AS n").single()["n"]
        print(f"\nпредпросмотр: будет помечено связей: {n} → schema_version='{STAMP}'")
        raise SystemExit(0)

    rel = s.run(f"""
        MATCH ()-[r]->() WHERE {SCOPE} AND r.layer = 'regex' AND r.schema_version IS NULL
        SET r.schema_version = $stamp,
            r.provenance_note = 'слой правил, записано до введения версий схемы'
        RETURN count(r) AS n
    """, stamp=STAMP).single()["n"]
    ent = s.run("""
        MATCH (e:Entity) WHERE e.layer = 'regex' AND e.schema_version IS NULL
        SET e.schema_version = $stamp,
            e.provenance_note = 'слой правил, записано до введения версий схемы'
        RETURN count(e) AS n
    """, stamp=STAMP).single()["n"]
    print(f"\nпомечено: связей {rel}, сущностей {ent}")

    after = s.run(f"""
        MATCH ()-[r]->() WHERE {SCOPE}
        RETURN coalesce(r.schema_version,'(пусто)') AS ver, count(r) AS n ORDER BY n DESC
    """)
    print("\nпосле правки, распределение версий у смысловых связей:")
    for r in after:
        print(f"   {r['ver']:<16} {r['n']}")
    left = s.run(f"MATCH ()-[r]->() WHERE {SCOPE} AND r.schema_version IS NULL RETURN count(r) AS n").single()["n"]
    print(f"\nосталось без версии: {left}")
drv.close()
