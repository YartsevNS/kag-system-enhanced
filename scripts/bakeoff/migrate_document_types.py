"""Миграция видов документов в словарь v0 (утверждён владельцем 10.10.2026).

ЧТО ДЕЛАЕТ. Переводит значения `documents.document_type` из старого набора
(standard, news, legal, order, contract, invoice, letter, report, policy, financial,
technical, medical, other, identity) в коды словаря v0 (src/indexing/document_kinds.py).

КАК МЕНЯЕТ: через админский эндпоинт ручной правки разметки
(`POST /admin/models/document-classification`) — он обновляет и запись в БД, и payload Qdrant
(иначе фильтры поиска не увидят новое значение), и пишет в журнал действий «кто/что/зачем».
Никаких прямых UPDATE по типу: правка должна быть проверяемой.

ЧЕГО НЕ ДЕЛАЕТ: не угадывает там, где соответствие неоднозначное. Заголовок не подошёл ни под
одно правило → документ попадает в раздел «НА РЕВЬЮ», тип остаётся прежним (лучше видимый код
старого словаря, чем выдуманный новый вид).

МАРШРУТ НОВОСТЕЙ. У видов `news` (29 документов) маршрут коллекции держался на самом значении типа:
`is_news_document` в этом случае отправлял документ в `kag_news`. После перевода в `publication`
тип перестаёт быть признаком маршрута (это и есть смысл правки 10.10.2026), поэтому для этих
документов ДОПОЛНИТЕЛЬНО ставится явный признак `documents.collection = 'news'` — тем же значением,
которым маршрут определялся до миграции, чтобы путь векторов не изменился.

ЗАПУСК в контейнере api (файл кладётся в bind-каталог ./data, в контейнере — /app/data):
  docker exec kag-api python /app/data/migrate_document_types.py             # сухой прогон
  docker exec kag-api python /app/data/migrate_document_types.py --apply     # применить
Отчёт всегда пишется в /app/data/migrate_document_types_report.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request

API = os.environ.get("KAG_API", "http://127.0.0.1:8000/api/v1")
REPORT_PATH = os.environ.get("REPORT_PATH", "/app/data/migrate_document_types_report.json")

# Значения старого набора, у которых соответствие ОДНОЗНАЧНОЕ (без оглядки на заголовок).
ONE_TO_ONE = {
    "contract": "contract",
    "invoice": "invoice",
    "act": "act",
    "order": "subordinate_act",
    "legal": "law",
    "policy": "regulation",
    "letter": "official_letter",
    "report": "report",
    "form": "form_template",
    "other": "other",
}

# Где одной таблицей не обойтись: вид уточняется по ЗАГОЛОВКУ. Первое совпадение выигрывает.
# Якоря не случайны: заголовок «Требования к защите информации в соответствии с ГОСТ Р 56545—2015…»
# содержит «ГОСТ», но национальным стандартом НЕ является — он должен уйти на ревью.
TITLE_RULES = {
    "standard": [
        (r"^\s*пнст|предварительный\s+национальный\s+стандарт", "preliminary_standard"),
        (r"^\s*гост", "national_standard"),
        (r"сто\s*бр|стандарт\s+организации|стандарт\s+банка", "org_standard"),
        (r"^\s*ту\b|технические\s+условия", "specification"),
        (r"^\s*сп\s*\d|свод\s+правил", "code_of_practice"),
        (r"^\s*р\s*\d|рекомендации\s+по\s+стандартизации", "standardization_recommendation"),
    ],
    "news": [(r".*", "publication")],
    "financial": [
        (r"квитанц|подтверждени\w*\s+оплаты|чек\b", "invoice"),
        (r".*", "reference"),
    ],
    "technical": [
        (r"спецификаци|технические\s+условия", "specification"),
        (r"инструкци|руководство", "instruction"),
        (r".*", "reference"),
    ],
    "medical": [
        (r"инструкци", "instruction"),
        (r".*", "reference"),
    ],
    "identity": [(r".*", "form_template")],
}

# Виды, которые после перевода в v0 ОБЯЗАНЫ получить явный маршрут коллекции.
# Значение признака = ответ is_news_document ДО миграции, ни одного документа не переносим.
COLLECTION_MARK = {"news": "news"}


def login() -> str:
    req = urllib.request.Request(
        API + "/auth/login",
        data=json.dumps({
            "username": os.environ.get("ADMIN_USERNAME", "admin"),
            "password": os.environ["ADMIN_PASSWORD"],
        }).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req).headers.get("Set-Cookie", "").split(";")[0]


def get(path: str, cookie: str) -> dict:
    req = urllib.request.Request(API + path, headers={"Cookie": cookie})
    return json.load(urllib.request.urlopen(req, timeout=120))


def post(path: str, body: dict, cookie: str) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Cookie": cookie, "Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=60))


def target_kind(old: str, title: str):
    """(новый код, приём) или (None, причина) — где соответствие не доказано."""
    if old in ONE_TO_ONE:
        return ONE_TO_ONE[old], "таблица 1:1"
    rules = TITLE_RULES.get(old)
    if not rules:
        return None, f"значение «{old}» не описано правилами"
    for pattern, new in rules:
        if re.search(pattern, title or "", re.IGNORECASE | re.UNICODE):
            return new, f"правило «{pattern}»"
    return None, f"для «{old}» ни одно правило не подошло"


def main() -> int:
    from src.indexing.document_kinds import is_valid  # noqa: PLC0415 — нужен путь контейнера

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="применить (по умолчанию сухой прогон)")
    ap.add_argument("--limit", type=int, default=2000)
    args = ap.parse_args()

    ck = login()
    docs = get(f"/upload/list?limit={args.limit}", ck).get("documents") or []
    print(f"документов получено: {len(docs)}")

    already, skipped, changes, review = 0, 0, [], []
    for d in docs:
        did = d.get("document_id") or ""
        old = str(d.get("document_type") or "").strip()
        title = (d.get("recognized_title") or d.get("filename") or "").strip()
        if not did:
            continue
        if old and is_valid(old):
            already += 1
            continue
        if not old:
            review.append({"document_id": did, "title": title, "old": "",
                           "why": "вид не заполнен — это не «выдуманный вид», а отсутствие разметки"})
            continue
        new, why = target_kind(old, title)
        if not new:
            review.append({"document_id": did, "title": title, "old": old, "why": why})
            continue
        if new == old:
            skipped += 1
            continue
        changes.append({"document_id": did, "title": title, "old": old, "new": new,
                        "why": why, "collection": COLLECTION_MARK.get(old, "")})

    # Отчёт по парам «было → стало»: это то, что владелец смотрит перед применением.
    pairs: dict = {}
    for c in changes:
        pairs.setdefault(f"{c['old']} → {c['new']}", 0)
        pairs[f"{c['old']} → {c['new']}"] += 1
    print("\n=== ЧТО БУДЕТ ИЗМЕНЕНО ===")
    for k, v in sorted(pairs.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>4}  {k}")
    print(f"\nуже в словаре v0: {already}; без изменений: {skipped}; к правке: {len(changes)}; "
          f"на ревью: {len(review)}")
    if review:
        print("\n=== НА РЕВЬЮ (тип НЕ меняем) ===")
        for r in review[:40]:
            print(f"  {r['old'] or '(пусто)':>12}  {r['title'][:70]}  ← {r['why']}")

    report = {"changes": changes, "review": review, "pairs": pairs,
              "counts": {"already": already, "skipped": skipped,
                         "changes": len(changes), "review": len(review)}}
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nотчёт: {REPORT_PATH}")

    if not args.apply:
        print("СУХОЙ ПРОГОН: ничего не записано (для применения — флаг --apply)")
        return 0

    ok = fail = 0
    for c in changes:
        try:
            res = post("/admin/models/document-classification", {
                "document_id": c["document_id"],
                "document_type": c["new"],
                "note": (f"миграция в словарь v0 (10.10.2026): было «{c['old']}», стало «{c['new']}»; "
                         f"основание — {c['why']}"),
            }, ck)
            if res.get("status") != "ok":
                fail += 1
                print(f"   отказ {c['document_id'][:12]}: {str(res.get('message'))[:90]}")
                continue
            ok += 1
        except Exception as e:  # noqa: BLE001 — один документ не должен ронять прогон
            fail += 1
            print(f"   ошибка {c['document_id'][:12]}: {type(e).__name__}: {str(e)[:90]}")
            continue
        # Явный маршрут коллекции: пишем ОТДЕЛЬНО от типа и только тем значением, которым
        # маршрут определялся до миграции. Если запись не удалась — говорим вслух, а не молчим:
        # иначе новость уедет в общую коллекцию при следующей переобработке.
        if c["collection"]:
            try:
                from src.api.services.document_repository import get_doc_repo  # noqa: PLC0415
                get_doc_repo().upsert(c["document_id"], {"collection": c["collection"]})
            except Exception as e:  # noqa: BLE001
                print(f"   ВНИМАНИЕ: маршрут коллекции не записан для "
                      f"{c['document_id'][:12]}: {type(e).__name__}: {e}")

    print(f"\nзаписано: {ok}, ошибок: {fail}")

    # Проверка: значения в базе ТОЛЬКО из словаря (кроме оставленных на ревью).
    acts = get("/admin/models/document-actions?limit=5", ck)
    print("последние записи журнала действий:",
          [(a.get("target", "")[:8], (a.get("details") or {}).get("values", {}).get("document_type"))
           for a in (acts.get("items") or [])][:5])
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
