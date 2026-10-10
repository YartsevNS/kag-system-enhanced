"""Приведение связей «связано с» к новому правилу: пары внутри ОДНОГО фрагмента.

Что изменилось в коде: связи RELATED_TO строятся теперь только для сущностей, упомянутых в ОДНОМ
фрагменте (было — любые пары внутри документа, квадратичный рост). Старые данные ещё содержат
«документные» пары.

Что делает прибор: удаляет связи RELATED_TO, у которых НЕТ ни одного общего фрагмента, то есть
оставляет только «рядом по тексту». Другие типы связей не трогает. Сначала считает и показывает,
потом (по команде) удаляет.

Оценка (замер 09.10.2026, 279 документов): всего 125 607 связей, из них RELATED_TO ~74% (≈93 тыс.).
После чистки останется меньше: точное число печатает предпросмотр.

Запуск в контейнере api:
  docker exec kag-api python /app/data/cleanup_document_level_links.py --dry-run
  docker exec kag-api python /app/data/cleanup_document_level_links.py
"""
import os
import sys

from neo4j import GraphDatabase

uri = os.environ.get("NEO4J_URI") or f"bolt://{os.environ.get('NEO4J_HOST', 'neo4j')}:{os.environ.get('NEO4J_PORT', 7687)}"
drv = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))
dry = "--dry-run" in sys.argv

# «документная» пара: сущности связаны, но НИ ОДИН фрагмент не упоминает обеих сразу
COUNT_DOCUMENT_LEVEL = """
MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity)
WHERE NOT EXISTS {
    MATCH (c:Chunk)-[:MENTIONS]->(a), (c)-[:MENTIONS]->(b)
}
RETURN count(r) AS n
"""

COUNT_INTRA_CHUNK = """
MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity)
WHERE EXISTS {
    MATCH (c:Chunk)-[:MENTIONS]->(a), (c)-[:MENTIONS]->(b)
}
RETURN count(r) AS n
"""

with drv.session() as s:
    total = s.run("MATCH ()-[r:RELATED_TO]->() RETURN count(r) AS n").single()["n"]
    doc_level = s.run(COUNT_DOCUMENT_LEVEL).single()["n"]
    intra = s.run(COUNT_INTRA_CHUNK).single()["n"]
    all_rels = s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
    print(f"связей всего: {all_rels}; из них RELATED_TO: {total}")
    print(f"   внутри фрагмента (оставляем): {intra}")
    print(f"   только «в одном документе» (кандидаты на удаление): {doc_level}")

    if dry:
        print("\nэто предпросмотр. После удаления останутся пары, встречающиеся в одном фрагменте;")
        print("факт «сущности в одном документе» остаётся доступен через связи Chunk-MENTIONS.")
        raise SystemExit(0)

    deleted = s.run("""
        MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity)
        WHERE NOT EXISTS {
            MATCH (c:Chunk)-[:MENTIONS]->(a), (c)-[:MENTIONS]->(b)
        }
        DELETE r
        RETURN count(r) AS n
    """).single()["n"]
    print(f"\nудалено связей «только в документе»: {deleted}")
    left = s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
    left_rel = s.run("MATCH ()-[r:RELATED_TO]->() RETURN count(r) AS n").single()["n"]
    print(f"осталось связей всего: {left} (из них RELATED_TO {left_rel})")
drv.close()
