"""Проверка графа по документам: сколько узлов легло, с какими версиями, сколько связей.

Зачем прибор: «задача поставлена» и «узлы прибавились» — разные факты. Здесь по каждому документу
смотрим фрагменты в графе, наличие наших новых полей (schema_version — версия словаря,
extractor_version — отпечаток промпта извлечения) и число связей «фрагмент → сущность». Ноль по любому
из них означает, что граф построен частично, и это видно числом, а не по журналу.

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/graph_verify_docs.py --limit 6
"""
from __future__ import annotations

import argparse
import sys


def q(query: str):
    from src.indexing.knowledge_graph import kg_service

    res = kg_service.execute_cypher(query)
    if isinstance(res, dict):
        return res.get("data") or res.get("rows") or []
    return res or []


def scalar(query: str):
    rows = q(query)
    if rows and isinstance(rows[0], dict):
        return list(rows[0].values())[0]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--all", action="store_true", help="проверить весь корпус, а не последние документы")
    args = ap.parse_args()

    from sqlalchemy import text

    from src.database.session import get_session_local

    sess = get_session_local()()
    if args.all:
        rows = sess.execute(text(
            "select id, filename, chunks_count from documents where chunks_count > 0 "
            "order by filename")).fetchall()
    else:
        rows = sess.execute(text(
            "select id, filename, chunks_count from documents where chunks_count > 0 "
            "order by created_at desc limit :n"), {"n": args.limit}).fetchall()
    sess.close()

    total_chunks = int(scalar("MATCH (n:Chunk) RETURN count(n) AS c") or 0)
    print(f"всего фрагментов в графе: {total_chunks}; проверяю документов: {len(rows)}\n")
    print(f"{'документ':<44} {'в базе':>7} {'в графе':>8} {'вер/сх':>7} {'вер/извл':>9} {'упомин.':>8}")
    bad = []
    for did, name, chunks in rows:
        # Фрагменты связаны с документом ОТНОШЕНИЕМ HAS_CHUNK, а не свойством document_id:
        # у узла Chunk ключ — id фрагмента. Запрос «по свойству» возвращает ноль на любом документе
        # и читается как «граф не построен», хотя граф есть.
        in_graph = int(scalar(f"MATCH (d:Document {{id: '{did}'}})-[:HAS_CHUNK]->(c:Chunk) "
                              f"RETURN count(c) AS c") or 0)
        sv = int(scalar(f"MATCH (d:Document {{id: '{did}'}})-[:HAS_CHUNK]->(c:Chunk) "
                        f"RETURN count(c.schema_version) AS c") or 0)
        ev = int(scalar(f"MATCH (d:Document {{id: '{did}'}})-[:HAS_CHUNK]->(c:Chunk) "
                        f"RETURN count(c.extractor_version) AS c") or 0)
        rels = int(scalar(f"MATCH (d:Document {{id: '{did}'}})-[:HAS_CHUNK]->(c:Chunk)"
                          f"-[:MENTIONS]->(e) RETURN count(e) AS c") or 0)
        flag = ""
        if in_graph < int(chunks):
            flag = "  ← граф неполный"
            bad.append(str(name))
        print(f"{str(name)[:42]:<44} {chunks:>7} {in_graph:>8} {sv:>7} {ev:>9} {rels:>8}{flag}")

    print(f"\nбез полного графа: {len(bad)}")
    for b in bad[:10]:
        print(f"  {b[:80]}")

    print("\nпримеры связей последнего проверенного документа:")
    if rows:
        did = rows[0][0]
        for r in q(f"MATCH (d:Document {{id: '{did}'}})-[:HAS_CHUNK]->(c:Chunk)"
                   f"-[r:MENTIONS]->(e) RETURN e.type AS t, count(e) AS c ORDER BY c DESC LIMIT 5"):
            print(f"  {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
