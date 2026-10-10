"""Перенести разметку модели в базу там, где собственный классификатор МОЛЧИТ.

Правило: трогаем только документы, у которых в базе тип «other»/пустой (классификатор не ответил),
и только там, где модель уверена (уверенность ≥ порога и согласованность фрагментов ≥ порога).
Пишем через админский эндпоинт правки разметки — он обновляет и запись, и payload Qdrant,
и оставляет след в журнале действий (кто/что/зачем), то есть разметка становится ПРОВЕРЯЕМОЙ,
а не «так решила модель».

Запуск в контейнере api (файл меток кладётся в bind-каталог):
  docker exec kag-api python /app/data/apply_doc_labels.py --labels /app/data/labels_v0_docs.json --dry-run
  docker exec kag-api python /app/data/apply_doc_labels.py --labels /app/data/labels_v0_docs.json
"""
import argparse
import json
import os
import urllib.request
from pathlib import Path

API = os.environ.get("KAG_API", "http://127.0.0.1:8000/api/v1")
NO_ANSWER = {"", "other", "unknown", "(пусто)"}

# Словарь v0 — НАШ проект словаря, а в базе живёт ДРУГОЙ набор значений (standard, news, legal, order,
# contract, invoice, letter, report, policy, financial, technical, medical, other, identity).
# Пока владелец не утвердил новый словарь как основной, пишем БЛИЖАЙШЕЕ значение из существующего
# набора: иначе в базе появится тип, которого не знают ни фильтры интерфейса, ни анализатор.
# Наш код при этом сохраняется в журнале действий (то есть не теряется).
DB_TYPE_MAP = {
    "publication": "news",
    "national_standard": "standard",
    "standardization_recommendation": "standard",
    "org_standard": "standard",
    "specification": "standard",
    "code_of_practice": "standard",
    "law": "legal",
    "subordinate_act": "order",
    "regulation": "policy",
    "instruction": "policy",
    "directive": "order",
    "methodology": "policy",
    "official_letter": "letter",
    "contract": "contract",
    "invoice": "invoice",
    "act": "invoice",
    "report": "report",
    "analytics": "report",
    "reference": "report",
}


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="/app/data/labels_v0_docs.json")
    ap.add_argument("--min-confidence", type=float, default=0.8)
    ap.add_argument("--min-consistency", type=float, default=0.67)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    targets = [r for r in rows
               if str(r.get("db_kind")) in NO_ANSWER
               and float(r.get("confidence") or 0) >= args.min_confidence
               and float(r.get("consistency") or 0) >= args.min_consistency]
    print(f"документов к разметке: {len(targets)} (из {len(rows)} в сводке)")
    for r in targets[:12]:
        print(f"   {r['document_id'][:12]} фрагментов {r['chunks']} | модель: {r['kind']:<28} "
              f"увер {r['confidence']} согласованность {r['consistency']} | в базе: {r['db_kind']}")
    if args.dry_run:
        print("\nпредпросмотр: записи не будет")
        return 0
    if not targets:
        return 0

    ck = login()
    ok = fail = 0
    for r in targets:
        try:
            db_type = DB_TYPE_MAP.get(r["kind"], r["kind"])
            res = post("/admin/models/document-classification", {
                "document_id": r["document_id"],
                "document_type": db_type,
                "note": (f"разметка моделью (JEV, словарь v0): код «{r['kind']}» → тип базы "
                         f"«{db_type}»; уверенность {r['confidence']}, согласованность фрагментов "
                         f"{r['consistency']}, фрагментов {r['chunks']}"),
            }, ck)
            if res.get("status") == "ok":
                ok += 1
            else:
                fail += 1
                print(f"   отказ {r['document_id'][:12]}: {str(res.get('message'))[:80]}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"   ошибка {r['document_id'][:12]}: {type(e).__name__}: {str(e)[:80]}")
    print(f"\nзаписано: {ok}, ошибок: {fail}")
    acts = json.load(urllib.request.urlopen(
        urllib.request.Request(API + "/admin/models/document-actions?limit=3", headers={"Cookie": ck}), timeout=30))
    print("последние записи журнала действий:",
          [(a.get("target", "")[:8], (a.get("details") or {}).get("values", {}).get("document_type"))
           for a in (acts.get("items") or [])][:3])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
