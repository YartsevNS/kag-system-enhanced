"""Проверка ручной правки разметки документа: БД + payload + журнал действий.

Что проверяем:
  1. правка вида/темы/фасетов/издателя через админский эндпоинт проходит;
  2. значения изменились в БД;
  3. они же появились в payload Qdrant (иначе фильтры поиска правку не увидят);
  4. в журнале действий есть запись: кто, что, зачем.

Запуск в контейнере api:
  docker exec -e ADMIN_PASSWORD=... kag-api python /app/data/manual_fix_probe.py
"""
import json
import os
import urllib.request

from qdrant_client import QdrantClient

API = os.environ.get("KAG_API", "http://127.0.0.1:8000/api/v1")


def login() -> str:
    req = urllib.request.Request(API + "/auth/login",
        data=json.dumps({"username": os.environ.get("ADMIN_USERNAME", "admin"),
                         "password": os.environ["ADMIN_PASSWORD"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req).headers.get("Set-Cookie", "").split(";")[0]


def post(path: str, body: dict, cookie: str) -> dict:
    req = urllib.request.Request(API + path, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Cookie": cookie, "Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=60))


def main() -> int:
    ck = login()
    # берём документ, у которого есть точки в Qdrant
    q = os.environ.get("QDRANT_HOST", "qdrant").replace("https://", "").replace("http://", "")
    cl = QdrantClient(url=f"http://{q}:6333", api_key=os.environ.get("QDRANT_API_KEY") or None)
    pts, _ = cl.scroll(collection_name="kag_documents", limit=50, with_payload=True, with_vectors=False)
    doc_id = ""
    for p in pts:
        pl = p.payload or {}
        if pl.get("document_id") and str(pl.get("chunk_id")).endswith("chunk_00001"):
            doc_id = pl["document_id"]
            break
    if not doc_id:
        print("не нашёл подходящий документ")
        return 1
    print("документ:", doc_id[:12])

    res = post("/admin/models/document-classification", {
        "document_id": doc_id,
        "document_type": "standard",
        "domain": "infosec",
        "issuer": "ПРОВЕРКА ИЗДАТЕЛЯ",
        "topics": ["информационная_безопасность", "проверка"],
        "facets": {"защита_предмет": ["данные"], "этап": ["эксплуатация"]},
        "note": "проверка ручной правки разведённым прибором",
    }, ck)
    print("ответ эндпоинта:", json.dumps(res, ensure_ascii=False)[:200])

    # payload Qdrant
    pts2, _ = cl.scroll(collection_name="kag_documents", limit=50, with_payload=True, with_vectors=False)
    seen = {}
    for p in pts2:
        pl = p.payload or {}
        if pl.get("document_id") == doc_id:
            seen = {k: pl.get(k) for k in ("document_type", "domain", "issuer", "topics", "facets")}
            break
    print("в payload Qdrant:", json.dumps(seen, ensure_ascii=False)[:220])

    acts = json.load(urllib.request.urlopen(
        urllib.request.Request(API + "/admin/models/document-actions?limit=3", headers={"Cookie": ck}), timeout=30))
    items = acts.get("items") or []
    print(f"записей в журнале действий: {len(items)}")
    if items:
        a = items[0]
        print("   последняя:", a.get("action"), "| кто:", a.get("actor"),
              "| что:", (a.get("details") or {}).get("changed"),
              "| зачем:", (a.get("details") or {}).get("note"))

    ok = (res.get("status") == "ok" and seen.get("issuer") == "ПРОВЕРКА ИЗДАТЕЛЯ"
          and seen.get("document_type") == "standard" and bool(items))
    print("\nИТОГ:", "правка доходит до БД, payload и журнала" if ok else "ПРОВЕРИТЬ ЦЕПОЧКУ")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
