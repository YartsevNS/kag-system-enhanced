"""Пометка фактов, сделанных ДО версионирования схемы.

Зачем. Версии схемы и извлекателя пишутся только у новых фактов. У всего, что уже лежит в графе,
их нет — и это опасно: «нет версии» неотличимо от «версию забыли записать». Тогда при следующей
смене словаря непонятно, что переобрабатывать, и приходится перебирать весь корпус.

Правильно: явно пометить старые факты как сделанные ДО введения версионирования (`legacy`).
После этого картина полная и однозначная: у каждого факта есть либо настоящая версия, либо
пометка «сделан до 2026.10.1, правила неизвестны».

Порядок: сначала показать, что будет помечено (--dry-run), потом выполнить.

Запуск в контейнере api или worker:
  docker exec kag-api python /app/data/mark_legacy_facts.py --dry-run
  docker exec kag-api python /app/data/mark_legacy_facts.py
"""
import os
import sys

from neo4j import GraphDatabase

LEGACY = "legacy"
uri = os.environ.get("NEO4J_URI") or f"bolt://{os.environ.get('NEO4J_HOST', 'neo4j')}:{os.environ.get('NEO4J_PORT', 7687)}"
drv = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))
dry = "--dry-run" in sys.argv

with drv.session() as s:
    stats = s.run("""
        MATCH (e:Entity)
        WITH count(e) AS vsego,
             count(CASE WHEN e.schema_version IS NULL THEN 1 END) AS bez_versii
        RETURN vsego, bez_versii
    """).single()
    rstats = s.run("""
        MATCH ()-[r]->()
        WHERE NOT type(r) IN ['MENTIONS','HAS_CHUNK','HAS_SECTION','SECTION_CHUNK']
        WITH count(r) AS vsego,
             count(CASE WHEN r.schema_version IS NULL THEN 1 END) AS bez_versii
        RETURN vsego, bez_versii
    """).single()
    print(f"сущности: {stats['vsego']}, без версии: {stats['bez_versii']}")
    print(f"содержательные связи: {rstats['vsego']}, без версии: {rstats['bez_versii']}")

    if dry:
        print("\nэто предпросмотр: перечисленные факты получат schema_version='legacy'")
        raise SystemExit(0)

    e = s.run("""
        MATCH (e:Entity) WHERE e.schema_version IS NULL
        SET e.schema_version = $v, e.extractor_version = $v, e.marked_at = datetime()
        RETURN count(e) AS n
    """, v=LEGACY).single()["n"]
    r = s.run("""
        MATCH ()-[r]->()
        WHERE r.schema_version IS NULL
          AND NOT type(r) IN ['MENTIONS','HAS_CHUNK','HAS_SECTION','SECTION_CHUNK']
        SET r.schema_version = $v, r.extractor_version = $v,
            r.created_at = coalesce(r.created_at, datetime())
        RETURN count(r) AS n
    """, v=LEGACY).single()["n"]
    print(f"\nпомечено сущностей: {e}, связей: {r}")

    left = s.run("""
        MATCH (e:Entity) WHERE e.schema_version IS NULL RETURN count(e) AS n
    """).single()["n"]
    leftr = s.run("""
        MATCH ()-[r]->() WHERE r.schema_version IS NULL
          AND NOT type(r) IN ['MENTIONS','HAS_CHUNK','HAS_SECTION','SECTION_CHUNK']
        RETURN count(r) AS n
    """).single()["n"]
    print(f"осталось без версии: сущностей {left}, связей {leftr}")

    mix = s.run("""
        MATCH (e:Entity) WITH e.schema_version AS v, count(e) AS n
        RETURN v, n ORDER BY n DESC LIMIT 6
    """)
    print("распределение по версиям у сущностей:", [(rec["v"], rec["n"]) for rec in mix])
drv.close()
