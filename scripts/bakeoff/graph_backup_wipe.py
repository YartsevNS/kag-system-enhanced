"""Слепок графа и его очистка — перед пересборкой на правильной онтологии.

Зачем сохраняем: очистка графа необратима, а связи строятся за токены. Слепок позволяет
посмотреть, что было, и сравнить с тем, что получится. Храним только смысл (имена сущностей,
их типы и связи), без служебных полей — этого достаточно для сравнения и разбора.

Запуск на стенде:
    docker exec kag-api python /app/data/graph_backup_wipe.py --backup      # только слепок
    docker exec kag-api python /app/data/graph_backup_wipe.py --wipe        # слепок + очистка
"""

from __future__ import annotations

import argparse
import json
import os
import time


def driver():
    from neo4j import GraphDatabase

    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
    )


def counts(session) -> dict:
    labels = {r["l"]: r["n"] for r in session.run(
        "MATCH (n) UNWIND labels(n) AS l RETURN l, count(*) AS n ORDER BY n DESC")}
    rels = {r["t"]: r["n"] for r in session.run(
        "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS n ORDER BY n DESC")}
    return {"узлы": labels, "связи": rels}


def backup(session, path: str) -> dict:
    entities = [{"n": r["name"], "t": r["type"], "c": r["confidence"]}
                for r in session.run("MATCH (e:Entity) RETURN e.name AS name, e.type AS type, "
                                     "e.confidence AS confidence")]
    relations = [{"s": r["s"], "r": r["r"], "t": r["t"]}
                 for r in session.run("MATCH (a:Entity)-[x]->(b:Entity) "
                                      "RETURN a.name AS s, type(x) AS r, b.name AS t")]
    documents = [{"f": r["f"], "id": r["id"]}
                 for r in session.run("MATCH (d:Document) RETURN d.filename AS f, d.id AS id")]
    payload = {"снято": time.strftime("%Y-%m-%d %H:%M"), "сущности": entities,
               "связи": relations, "документы": documents}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return {"сущностей": len(entities), "связей": len(relations), "документов": len(documents)}


def wipe(session) -> None:
    # Пакетами: одиночный DETACH DELETE на десятках тысяч узлов валит транзакцию по памяти.
    session.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS")
    session.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true", help="после слепка очистить граф")
    ap.add_argument("--backup", action="store_true", help="только слепок")
    ap.add_argument("--path", default="/app/data/graph_backup_before_ontology.json")
    args = ap.parse_args()

    drv = driver()
    with drv.session() as session:
        before = counts(session)
        print("=== ДО ===")
        print("  узлы:", before["узлы"])
        print("  связи:", dict(list(before["связи"].items())[:8]), "…" if len(before["связи"]) > 8 else "")

        info = backup(session, args.path)
        print(f"слепок сохранён: {args.path} — {info}")

        if args.wipe:
            print("=== ОЧИСТКА ===")
            t0 = time.time()
            wipe(session)
            after = counts(session)
            print(f"  готово за {round(time.time() - t0, 1)} с; осталось узлов "
                  f"{sum(after['узлы'].values())}, связей {sum(after['связи'].values())}")
    drv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
