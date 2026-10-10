"""Сверка документа по слоям: реестр ↔ Qdrant ↔ граф. Что именно отсутствует.

Зачем: в реестре стоит «фрагментов 1», граф при построении отвечает `no_chunks`, и снаружи это выглядит
как «граф не строится». На самом деле вопрос в другом: есть ли у документа векторы вообще. Прибор
спрашивает по каждому слою отдельно и печатает три числа — по ним видно, где обрыв.

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/doc_layers_check.py --docs id1,id2
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True, help="id документов через запятую")
    args = ap.parse_args()

    from sqlalchemy import text

    from src.config import get_settings
    from src.database.session import get_session_local
    from src.indexing.knowledge_graph import kg_service

    ids = [d.strip() for d in args.docs.split(",") if d.strip()]
    s = get_settings()
    base = f"http://{s.QDRANT_HOST}:{s.QDRANT_PORT}"
    headers = {"Content-Type": "application/json"}
    if getattr(s, "QDRANT_API_KEY", ""):
        headers["api-key"] = s.QDRANT_API_KEY

    sess = get_session_local()()
    print(f"{'документ':<44} {'колл.':>6} {'реестр':>7} {'qdrant':>7} {'граф':>6}")
    for did in ids:
        row = sess.execute(text(
            "select filename, chunks_count, status, coalesce(collection,'') from documents "
            "where id = :d"), {"d": did}).first()
        name, chunks, status, collection = (row or ("—", 0, "нет", ""))
        total_points = 0
        for coll in ("kag_documents", "kag_news"):
            body = json.dumps({"filter": {"must": [{"key": "document_id", "match": {"value": did}}]},
                               "limit": 1, "with_payload": False}).encode()
            req = urllib.request.Request(f"{base}/collections/{coll}/points/scroll", body,
                                         headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.loads(r.read().decode())["result"]
                total_points += int(res.get("points_count") or 0) or len(res.get("points") or [])
            except Exception as e:  # noqa: BLE001
                print(f"  (Qdrant {coll}: {type(e).__name__} {str(e)[:60]})")
        g = kg_service.execute_cypher(
            f"MATCH (d:Document {{id: '{did}'}})-[:HAS_CHUNK]->(c:Chunk) RETURN count(c) AS c")
        if isinstance(g, dict):
            g = g.get("data") or []
        in_graph = (g[0].get("c") if g and isinstance(g[0], dict) else 0) or 0
        print(f"{str(name)[:42]:<44} {collection or '—':>6} {chunks:>7} {total_points:>7} {in_graph:>6}")
    sess.close()
    print("\nчтение: «реестр» — число фрагментов по базе, «qdrant» — сколько точек реально лежит,"
          "\n«граф» — сколько узлов связано отношением HAS_CHUNK. Обрыв виден по первому нулю слева.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
