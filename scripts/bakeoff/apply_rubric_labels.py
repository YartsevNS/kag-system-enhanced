"""Запись разметки тем и фасетов в базу (запуск В КОНТЕЙНЕРЕ api).

Правило: пишем ТОЛЬКО там, где модель уверена (≥ порога), и всегда через админский эндпоинт правки
разметки — он обновляет запись и payload и оставляет след в журнале действий (кто/что/зачем).
Ответы с уверенностью ниже порога не выбрасываются: они попадают в отчёт «на ревью», чтобы решение
принимал человек, а не порог.

Запуск:
  docker exec kag-api python /app/data/apply_rubric_labels.py --labels /app/data/labels_docs_v0.jsonl
  (по умолчанию сухой прогон; для записи — --apply)
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request

API = os.environ.get("KAG_API", "http://127.0.0.1:8000/api/v1")
REPORT = os.environ.get("REPORT", "/app/data/labels_docs_v0_report.json")


def login() -> str:
    req = urllib.request.Request(
        API + "/auth/login",
        data=json.dumps({"username": os.environ.get("ADMIN_USERNAME", "admin"),
                         "password": os.environ["ADMIN_PASSWORD"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=30).headers.get("Set-Cookie", "").split(";")[0]


def post(path: str, body: dict, ck: str) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Cookie": ck, "Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=60))


def main() -> int:
    from src.api.services.document_repository import get_doc_repo
    from src.indexing import document_facets as df
    from src.indexing import document_topics as dt

    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="/app/data/labels_docs_v0.jsonl")
    ap.add_argument("--min-confidence", type=float, default=0.8)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rows = []
    with open(args.labels, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    docs = get_doc_repo().get_all()
    to_write, review, skipped = [], [], 0
    for r in rows:
        if not r.get("ok"):
            continue
        did = r.get("document_id")
        d = docs.get(did)
        if not d:
            skipped += 1
            continue
        a = r.get("answers") or {}

        def pick(qname):
            aq = a.get(qname) or {}
            choice, conf = aq.get("choice"), aq.get("confidence")
            if not choice or not isinstance(conf, (int, float)):
                return None, 0.0
            return str(choice), float(conf)

        rubrics, facets, notes = [], {}, []
        r_code, r_conf = pick("тема")
        if r_code and dt.is_valid(r_code):
            if r_conf >= args.min_confidence:
                rubrics = [r_code]
                notes.append(f"тема {r_code} ({r_conf:.2f})")
            else:
                review.append({"document_id": did, "field": "rubrics", "value": r_code,
                               "confidence": r_conf, "title": r.get("title")})
        ps, ps_conf = pick("предмет_защиты")
        nf, nf_conf = pick("нормативность")
        if ps and df.is_valid_value("protection_subject", ps) and ps_conf >= args.min_confidence:
            facets["protection_subject"] = [ps]
            notes.append(f"предмет защиты {ps} ({ps_conf:.2f})")
        elif ps:
            review.append({"document_id": did, "field": "protection_subject", "value": ps,
                           "confidence": ps_conf, "title": r.get("title")})
        if nf and df.is_valid_value("normative_force", nf) and nf_conf >= args.min_confidence:
            facets["normative_force"] = [nf]
            notes.append(f"нормативность {nf} ({nf_conf:.2f})")
        elif nf:
            review.append({"document_id": did, "field": "normative_force", "value": nf,
                           "confidence": nf_conf, "title": r.get("title")})

        if not rubrics and not facets:
            continue
        to_write.append({"document_id": did, "rubrics": rubrics, "facets": facets,
                         "note": "разметка моделью (JEV, уровень документа): " + "; ".join(notes),
                         "before": {"rubrics": d.get("rubrics"), "facets": d.get("facets")}})

    print(f"строк разметки: {len(rows)}; к записи: {len(to_write)}; на ревью: {len(review)}; "
          f"нет в реестре: {skipped}")
    for w in to_write[:5]:
        print(f"   {w['document_id'][:12]} {w['note'][:90]}")
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump({"to_write": to_write, "review": review}, f, ensure_ascii=False, indent=2)
    print(f"отчёт: {REPORT}")

    if not args.apply:
        print("СУХОЙ ПРОГОН: ничего не записано (для применения — флаг --apply)")
        return 0

    ck = login()
    ok = fail = 0
    for w in to_write:
        body = {"document_id": w["document_id"], "note": w["note"]}
        if w["rubrics"]:
            body["rubrics"] = w["rubrics"]
        if w["facets"]:
            body["facets"] = w["facets"]
        try:
            res = post("/admin/models/document-classification", body, ck)
            if res.get("status") == "ok":
                ok += 1
            else:
                fail += 1
                print(f"   отказ {w['document_id'][:12]}: {str(res.get('message'))[:90]}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"   ошибка {w['document_id'][:12]}: {type(e).__name__}: {str(e)[:90]}")
    print(f"\nзаписано: {ok}, ошибок: {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
