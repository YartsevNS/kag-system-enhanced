"""Приведение типов связей в графе к закрытому словарю.

Зачем. Проверка живой базы показала, что модель придумывала типы связей на ходу: в графе лежат
ASSIGNED_TO, DEPENDS_ON, EVALUATES, INDEMNITY, PERFORMED_BY, RELATES_TO и даже русские обороты
(«борьба_против», «выслан_из», «против»). Промпт извлечения типы связей вообще не задаёт — отсюда
и разнобой. Новые факты теперь пишутся только с типами из словаря (проверка на записи), а старые
надо привести к тому же виду.

Что делает: для каждого типа вне словаря — создаёт ребро RELATED_TO между теми же узлами
(ФАКТ НЕ ТЕРЯЕТСЯ, «эти две сущности связаны» остаётся) и удаляет ребро выдуманного типа.
Структурные типы нашего кода (MENTIONS, HAS_CHUNK, HAS_SECTION, SECTION_CHUNK, SUPERSEDED_BY)
и словарные типы не трогает.

Порядок: сначала показать, что будет сделано (--dry-run), потом выполнить.

Запуск в контейнере api:
  docker exec kag-api python /app/data/cleanup_relation_types.py --dry-run
  docker exec kag-api python /app/data/cleanup_relation_types.py
"""
import os
import sys

from neo4j import GraphDatabase

ALLOWED = {"RELATED_TO", "SIGNED_BY", "DATED", "AMOUNT", "BELONGS_TO", "LOCATED_AT",
           "MENTIONS", "HAS_CHUNK", "HAS_SECTION", "SECTION_CHUNK", "SUPERSEDED_BY",
           "PART_OF", "NEW_EDITION_OF", "REFERENCES", "AMENDS", "REPEALS", "BASED_ON"}

uri = os.environ.get("NEO4J_URI") or f"bolt://{os.environ.get('NEO4J_HOST', 'neo4j')}:{os.environ.get('NEO4J_PORT', 7687)}"
drv = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))
dry = "--dry-run" in sys.argv

with drv.session() as s:
    rows = list(s.run("MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS n ORDER BY n DESC"))
    outside = [(r["t"], r["n"]) for r in rows if r["t"].upper() not in ALLOWED]
    print(f"всего типов связей: {len(rows)}; вне словаря: {len(outside)}")
    for t, n in outside:
        print(f"   {t:<26} {n}")
    if not outside:
        print("чистить нечего")
        raise SystemExit(0)
    if dry:
        print("\nэто предпросмотр: связи вне словаря будут превращены в RELATED_TO, факт сохранится")
        raise SystemExit(0)

    converted = 0
    for t, n in outside:
        # ребро создаём ДО удаления выдуманного — иначе факт потеряется при сбое
        res = s.run(f"""
            MATCH (a)-[r:`{t}`]->(b)
            MERGE (a)-[:RELATED_TO]->(b)
            WITH r
            DELETE r
            RETURN count(r) AS deleted
        """).single()
        deleted = res["deleted"] if res else 0
        converted += deleted
        print(f"   {t:<26} преобразовано в RELATED_TO: {deleted}")
    print(f"\nитого преобразовано связей: {converted}")

    rows2 = list(s.run("MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS n ORDER BY n DESC"))
    left = [(r["t"], r["n"]) for r in rows2 if r["t"].upper() not in ALLOWED]
    print("осталось вне словаря:", left if left else "ничего")
    print("всего связей:", s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"])
drv.close()
