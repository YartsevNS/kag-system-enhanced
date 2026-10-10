"""Перенос ЛЕГАСИ-доменов документа в темы (рубрики) словаря v0 — пункт A4 дорожной карты.

Зачем. Тема документа жила в двух местах и оба не годились для фильтров: свободные `topics` от
модели (слова, по ним ничего не построить) и легаси-поле `domain` прежней схемы
(infosec/accounting/legal/universal) — оно заполнено у 70 документов из 328 и молча использовалось
жёстким фильтром поиска. Теперь тема — это `documents.rubrics`: список КОДОВ словаря
(src/indexing/document_topics.py), многозначный и мягкий. Этот прибор переносит то, что уже
известно, в новую ось — без выдумки: незнакомое значение не угадывается, а попадает в отчёт.

Соответствие (легаси → рубрика v0):
    infosec    → infosec     (информационная безопасность)
    legal      → law         (юриспруденция)
    accounting → economics   (экономика и финансы: бухгалтерский учёт — ближайшая рубрика словаря)
    universal  → (пусто)     «универсальный» — это отсутствие темы, а не тема
    пусто      → (пусто)

Запись идёт через админский эндпоинт правки разметки: он обновляет запись, payload Qdrant и
пишет в журнал действий (кто/что/зачем).

Запуск в контейнере api (файл в bind-каталоге ./data):
  docker exec kag-api python /app/data/migrate_domain_to_rubrics.py            # сухой прогон
  docker exec kag-api python /app/data/migrate_domain_to_rubrics.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from collections import Counter

API = os.environ.get("KAG_API", "http://127.0.0.1:8000/api/v1")
REPORT_PATH = os.environ.get("REPORT_PATH", "/app/data/migrate_domain_to_rubrics_report.json")

DOMAIN_TO_RUBRIC = {
    "infosec": "infosec",
    "legal": "law",
    "accounting": "economics",
    "universal": "",   # явный «нет темы»
    "": "",
}


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
    from src.indexing import document_topics as topics

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать (по умолчанию сухой прогон)")
    args = ap.parse_args()

    docs = get_doc_repo().get_all()
    print(f"документов в реестре: {len(docs)}")

    already, changes, review = 0, [], []
    for did, d in docs.items():
        have = topics.normalize(d.get("rubrics"))
        if have:
            already += 1
            continue
        raw = str(d.get("domain") or "").strip().lower()
        if raw not in DOMAIN_TO_RUBRIC:
            review.append({"document_id": did, "domain": raw,
                           "title": str(d.get("recognized_title") or d.get("filename") or "")[:70],
                           "why": "значение домена не описано правилами"})
            continue
        new = DOMAIN_TO_RUBRIC[raw]
        if not new:
            continue          # нет темы — так и оставляем (не выдумываем)
        changes.append({"document_id": did, "domain": raw, "rubrics": [new],
                        "title": str(d.get("recognized_title") or d.get("filename") or "")[:70]})

    pairs = Counter(f"{c['domain']} → {','.join(c['rubrics'])}" for c in changes)
    print("\n=== ЧТО БУДЕТ ЗАПИСАНО В ТЕМЫ ===")
    for k, v in sorted(pairs.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>4}  {k}")
    print(f"\nуже с темами: {already}; к записи: {len(changes)}; на ревью: {len(review)}")
    if review:
        print("\n=== НА РЕВЬЮ (тему НЕ пишем) ===")
        for r in review[:20]:
            print(f"  domain={r['domain']!r}  {r['title']}  ← {r['why']}")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump({"changes": changes, "review": review, "pairs": dict(pairs),
                   "counts": {"already": already, "changes": len(changes), "review": len(review)}},
                  f, ensure_ascii=False, indent=2)
    print(f"\nотчёт: {REPORT_PATH}")

    if not args.apply:
        print("СУХОЙ ПРОГОН: ничего не записано (для применения — флаг --apply)")
        return 0

    ck = login()
    ok = fail = 0
    for c in changes:
        try:
            res = post("/admin/models/document-classification", {
                "document_id": c["document_id"],
                "rubrics": c["rubrics"],
                "note": (f"тема из легаси-домена (A4, 10.10.2026): domain «{c['domain']}» → "
                         f"рубрика {c['rubrics']}; источник — поле прежней схемы"),
            }, ck)
            if res.get("status") != "ok":
                fail += 1
                print(f"   отказ {c['document_id'][:12]}: {str(res.get('message'))[:90]}")
            else:
                ok += 1
        except Exception as e:  # noqa: BLE001 — один документ не должен ронять прогон
            fail += 1
            print(f"   ошибка {c['document_id'][:12]}: {type(e).__name__}: {str(e)[:90]}")
    print(f"\nзаписано: {ok}, ошибок: {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
