"""Проверка построения графа на новых документах: сколько узлов прибавилось, за какое время, с какими
версиями (schema_version / extractor_version).

Зачем прибор. «Граф включён» и «задача поставлена» не доказывают, что граф построился: задача идёт в
maintenance-очередь, а узлы появляются асинхронно. Прибор снимает ЧИСЛА до и после, ждёт прироста и
печатает, какие свойства появились у новых узлов и связей — то есть проверяет НАШИ новые наработки
(версия словаря и отпечаток промпта извлечения), а не просто факт «что-то добавилось».

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/graph_build_probe.py --docs id1,id2 [--wait 1800]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

API = "http://127.0.0.1:8000"


def login() -> str:
    req = urllib.request.Request(
        API + "/api/v1/auth/login",
        data=json.dumps({"username": os.environ.get("ADMIN_USERNAME", "admin"),
                         "password": os.environ.get("ADMIN_PASSWORD", "")}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers.get("Set-Cookie", "").split(";")[0]


def post(path: str, cookie: str, body: dict | None = None, timeout: int = 120):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(API + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Cookie": cookie})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {str(e)[:140]}"}


def q(query: str, **params):
    """Запрос к графу. Возвращает список словарей (контракт kg_service — список строк)."""
    from src.indexing.knowledge_graph import kg_service

    try:
        res = kg_service.execute_cypher(query)
    except TypeError:
        res = kg_service.execute_cypher(query, params or None)
    if isinstance(res, dict):
        return res.get("data") or res.get("rows") or []
    return res or []


def counts() -> dict:
    out = {}
    for label in ("Document", "Chunk", "Entity"):
        rows = q(f"MATCH (n:{label}) RETURN count(n) AS c")
        out[label] = rows[0].get("c") if rows and isinstance(rows[0], dict) else rows
    rows = q("MATCH ()-[r]->() RETURN count(r) AS c")
    out["Relations"] = rows[0].get("c") if rows and isinstance(rows[0], dict) else rows
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True, help="id документов через запятую")
    ap.add_argument("--wait", type=int, default=1800)
    ap.add_argument("--skip-graph", default="false", help="значение настройки system/graph.skip")
    args = ap.parse_args()

    ids = [d.strip() for d in args.docs.split(",") if d.strip()]
    cookie = login()
    before = counts()
    print(f"до: {before}")

    status, body = post("/api/v1/admin/models/graph-build-config", cookie,
                        {"skip": args.skip_graph.lower() == "true"})
    print(f"настройка построения графа при обработке: {body} (код {status})")

    status, body = post("/api/v1/kg/rebuild-graph", cookie, {"document_ids": ids})
    print(f"постановка перестроения: код {status} {json.dumps(body, ensure_ascii=False)[:200]}")
    if status not in (200, 202):
        return 1

    t0 = time.time()
    last = before
    while time.time() - t0 < args.wait:
        time.sleep(20)
        now = counts()
        grew = (now.get("Chunk") or 0) > (last.get("Chunk") or 0)
        print(f"  +{time.time()-t0:6.0f} с: {now}" + ("   ← прирост" if grew else ""))
        last = now
        # Признак завершения: узлы документа появились и перестали расти два круга подряд.
        if grew:
            time.sleep(20)
            after = counts()
            if after == now:
                last = after
                break
            last = after
    after = counts()
    print(f"\nпосле: {after}")
    print(f"прирост: Chunk {int(after.get('Chunk') or 0) - int(before.get('Chunk') or 0)}, "
          f"Entity {int(after.get('Entity') or 0) - int(before.get('Entity') or 0)}, "
          f"связей {int(after.get('Relations') or 0) - int(before.get('Relations') or 0)}; "
          f"время {time.time()-t0:.0f} с")

    print("\nпо документам (какие свойства легли на фрагменты):")
    for doc in ids:
        for prop in ("document_id", "doc_id", "documentId"):
            rows = q(f"MATCH (n:Chunk {{{prop}: $d}}) RETURN count(n) AS c", d=doc)
            if rows and isinstance(rows[0], dict) and rows[0].get("c"):
                print(f"  {doc[:8]} ({prop}): фрагментов {rows[0]['c']}")
                rows2 = q(f"MATCH (n:Chunk {{{prop}: $d}}) "
                          f"RETURN count(n.schema_version) AS sv, count(n.extractor_version) AS ev, "
                          f"count(n.access) AS acc", d=doc)
                print(f"      с версией схемы: {rows2[0].get('sv')}, с версией извлечения: "
                      f"{rows2[0].get('ev')}, с правами: {rows2[0].get('acc')}")
                break
        else:
            print(f"  {doc[:8]}: узлов фрагментов не найдено (проверить имя свойства)")

    rows = q("MATCH (n:Chunk) WHERE n.extractor_version IS NOT NULL RETURN n.extractor_version AS v, "
             "count(n) AS c ORDER BY c DESC LIMIT 5")
    print(f"\nотпечатки промпта извлечения в графе: {rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
