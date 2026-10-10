"""Прибор: записать в метаданные происхождение машинной разметки (уверенность и топ-варианты).

Зачем. Значения темы и фасетов ставит модель, и на части документов она колеблется. Чтобы человек
мог разбирать именно спорные случаи (страница «Спорные случаи»), в документе должно лежать ЧТО
ответила модель и НАСКОЛЬКО была уверена. Прибор читает результаты прогонов JEV и пишет только
провенанс — значения он НЕ меняет (иначе перезапишет ручные правки; сервер это тоже проверяет).

Сливает несколько прогонов: по каждому (документ, вопрос) берётся ответ с наибольшей уверенностью.
Это позволяет сложить «чемпион» и адаптивную формулировку в одну запись.

Запуск (внутри контейнера api):
  docker exec kag-api python /app/data/import_label_provenance.py \
      --labels /app/data/labels_docs_v0_labform.jsonl \
      --labels /app/data/jev_adaptive.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

API = os.environ.get("KAG_API", "http://127.0.0.1:8000/api/v1")
# Поля модели → поля происхождения. У темы и предмета защиты значения многозначны (все выше порога),
# у нормативности — один вариант: там вопрос закрытый.
QUESTIONS = {
    "тема": ("rubrics", True),
    "предмет_защиты": ("facets.protection_subject", True),
    "нормативность": ("facets.normative_force", False),
}
EXTRA_PROB = 0.4          # тот же порог дополнительных значений, что в применении разметки


def login() -> str:
    req = urllib.request.Request(
        API + "/auth/login",
        data=json.dumps({"username": os.environ.get("ADMIN_USERNAME", "admin"),
                         "password": os.environ["ADMIN_PASSWORD"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=30).headers.get("Set-Cookie", "").split(";")[0]


def post(path: str, body: dict, cookie: str) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Cookie": cookie, "Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=180))


def collect(paths) -> dict:
    """{(document_id, вопрос): (значение, уверенность, альтернативы)} — лучший ответ по уверенности."""
    best: dict = {}
    for path in paths:
        p = Path(path)
        if not p.exists():
            print(f"нет файла: {p}")
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not row.get("ok"):
                continue
            did = row.get("document_id")
            q = row.get("question") or ("тема" if "answers" in row else None)
            answers = row.get("answers") or {}
            if q is None:
                continue
            if answers:                      # формат разметчика: сразу три вопроса
                for qname, ans in answers.items():
                    _put(best, did, qname, ans)
            else:                            # формат лаборатории: один вопрос + вероятности
                _put(best, did, q, {"choice": row.get("choice"),
                                    "confidence": row.get("confidence"),
                                    "probabilities": row.get("probabilities")})
    return best


def _put(best: dict, did: str, qname: str, ans: dict) -> None:
    if qname not in QUESTIONS:
        return
    probs = ans.get("probabilities") or {}
    try:
        conf = float(ans.get("confidence")) if ans.get("confidence") is not None else None
    except (TypeError, ValueError):
        conf = None
    if conf is None and isinstance(probs, dict) and probs:
        conf = max(float(v) for v in probs.values())
    if conf is None:
        return
    key = (did, qname)
    prev = best.get(key)
    if prev and prev[1] >= conf:
        return
    alts = []
    if isinstance(probs, dict) and probs:
        alts = [[k, float(v)] for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:3]]
    best[key] = (ans.get("choice"), conf, alts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", action="append", default=[],
                    help="файл(ы) результатов JEV; можно указать несколько раз")
    ap.add_argument("--chunk", type=int, default=200, help="размер порции запроса")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    best = collect(args.labels or [])
    items = []
    for (did, qname), (choice, conf, alts) in sorted(best.items()):
        field, multi = QUESTIONS[qname]
        if multi:
            value = [c for c, p in alts if p >= EXTRA_PROB and c] or ([choice] if choice else [])
        else:
            value = choice
        items.append({"document_id": did, "field": field, "value": value,
                      "confidence": conf, "alternatives": alts})

    flagged = sum(1 for i in items if i["confidence"] < 0.6)
    print(f"записей о происхождении: {len(items)} (документов: {len({i['document_id'] for i in items})}); "
          f"ниже порога 0,6: {flagged}")
    if args.dry_run:
        for i in items[:5]:
            print("  ", i["document_id"][:8], i["field"], i["value"], round(i["confidence"], 2))
        print("сухой прогон: ничего не записано")
        return 0

    cookie = login()
    written = documents = skipped = missing = 0
    for start in range(0, len(items), max(1, args.chunk)):
        part = items[start:start + max(1, args.chunk)]
        res = post("/admin/models/label-provenance", {"items": part}, cookie)
        if res.get("status") != "ok":
            print(f"ошибка порции {start}: {res}")
            return 1
        written += res.get("written", 0)
        documents += res.get("documents", 0)
        skipped += res.get("skipped_manual", 0)
        missing += res.get("missing", 0)
        print(f"  порция {start + len(part)}/{len(items)} | записано {written} | ручных пропущено {skipped}")
    print(f"итог: записей {written}, документов {documents}, пропущено ручных {skipped}, "
          f"не найдено документов {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
