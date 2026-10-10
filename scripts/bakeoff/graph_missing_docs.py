"""Найти документы, у которых ФРАГМЕНТЫ в графе отсутствуют, и поставить им построение графа.

Зачем: «граф включён» не значит «граф есть у всех». Документы, обработанные в период, когда построение
было отключено, остаются без узлов — и их видно только сверкой реестра с графом. Прибор печатает
список и (по флагу) ставит построение по ПОЛНЫМ id: укороченные id дают 0/0 и выглядят как успех.

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/graph_missing_docs.py [--apply] [--limit 50]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def login() -> str:
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/auth/login",
        data=json.dumps({"username": os.environ.get("ADMIN_USERNAME", "admin"),
                         "password": os.environ.get("ADMIN_PASSWORD", "")}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers.get("Set-Cookie", "").split(";")[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="поставить построение графа для найденных")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    from sqlalchemy import text

    from src.database.session import get_session_local
    from src.indexing.knowledge_graph import kg_service

    def scalar(query: str):
        res = kg_service.execute_cypher(query)
        if isinstance(res, dict):
            res = res.get("data") or res.get("rows") or []
        if res and isinstance(res[0], dict):
            return list(res[0].values())[0]
        return res or 0

    sess = get_session_local()()
    rows = sess.execute(text(
        "select id, filename, chunks_count from documents where chunks_count > 0 "
        "order by created_at desc")).fetchall()
    sess.close()

    missing = []
    for did, name, chunks in rows:
        # Сверяем ЧИСЛА: сколько фрагментов у документа в реестре и сколько узлов связано в графе.
        in_graph = int(scalar(f"MATCH (d:Document {{id: '{did}'}})-[:HAS_CHUNK]->(c:Chunk) "
                              f"RETURN count(c) AS c") or 0)
        if in_graph < int(chunks):
            missing.append((did, name, chunks, in_graph))
        if len(missing) >= args.limit:
            break

    print(f"документов без полного графа: {len(missing)} (проверено {len(rows)})")
    for did, name, chunks, in_graph in missing:
        print(f"  {did[:8]}  в базе {chunks}, в графе {in_graph}  {str(name)[:60]}")

    if not missing or not args.apply:
        return 0

    ids = [did for did, _, _, _ in missing]
    cookie = login()
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/kg/rebuild-graph",
        data=json.dumps({"document_ids": ids}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Cookie": cookie})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            print(f"постановка: код {r.status} {r.read().decode()[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"постановка не удалась: {type(e).__name__}: {str(e)[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
