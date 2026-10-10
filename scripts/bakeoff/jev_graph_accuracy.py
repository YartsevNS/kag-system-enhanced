"""Оценка точности извлечения судьёй JEV: закрытые вопросы с вероятностями.

Зачем: точность графа у нас не измерялась никогда — неизвестно, сколько сущностей и связей извлечено
верно. Своим судьёй быть нельзя, поэтому судит отдельная модель (JEV через Polza), причём закрытыми
вопросами: «следует ли связь из текста?» (вероятность «да») и «какой тип связи подходит?» (выбор из
НАШЕЙ онтологии). Из ответов считаем точность: доля подтверждённых связей, доля верных типов,
и сколько связей судья отнёс к другому типу — это прямо показывает, где онтология не совпадает с текстом.

Ключ — ТОЛЬКО из окружения (POLZA_API_KEY или HERMES_CUSTOM_POLZA_API_KEY), в аргументах не передаём.

Запуск (на ноутбуке, по образцу, снятому на стенде):
    python scripts/bakeoff/jev_graph_accuracy.py <образец.json> [--limit 20]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://polza.ai/api/v1/systemone"

# Типы связей нашей онтологии (для вопроса «какой тип подходит»). Держим в согласии с
# src/indexing/graph_ontology.py: если там меняются типы, здесь надо обновить список.
RELATION_CHOICES = [
    "SIGNED_BY (подписано)", "ISSUED_BY (издано)", "DATED (датировано)", "AMOUNT (на сумму)",
    "LOCATED_AT (расположено)", "SUPERSEDES (отменяет)", "AMENDS (изменяет)",
    "REFERENCES (ссылается на)", "HAS_CLAUSE (содержит пункт)", "REQUIRES (устанавливает требование)",
    "APPLIES_TO (распространяется на)", "PART_OF (входит в состав)", "DEFINES (определяет)",
    "RELATED_TO (тип не определён)",
]
CODE_BY_LABEL = {c.split(" ")[0]: c for c in RELATION_CHOICES}


def post(body: dict, key: str, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def judge_chunk(item: dict, key: str) -> dict:
    """Один запрос на фрагмент: все связи сразу, закрытыми вопросами."""
    text = item.get("текст") or ""
    relations = item.get("связи") or []
    questions: dict = {}

    for i, r in enumerate(relations, 1):
        # 1. Следует ли связь из текста (вероятность «да»)
        questions[f"связь_{i}_есть"] = {
            "type": "noul",
            "instructions": (f"Следует ли из текста, что «{r['от']}» связан(о) с «{r['к']}» "
                             f"именно так, как указано (тип «{r['тип']}»)? Учитывай только то, "
                             f"что явно сказано в тексте."),
        }
        # 2. Какой тип связи подходит (выбор из нашей онтологии)
        questions[f"связь_{i}_тип"] = {
            "type": "choice",
            "instructions": (f"Между «{r['от']}» и «{r['к']}»: какой тип связи точнее всего "
                             f"описывает отношение по тексту?"),
            "criteria": {label: "" for label in RELATION_CHOICES},
        }
    if not questions:
        return {"answers": {}, "usage": {}}
    return post({"model": "typesafe/jev", "state": text[:6000], "questions": questions}, key)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample", help="файл образца, снятый dump_extraction_sample.py")
    ap.add_argument("--limit", type=int, default=20, help="сколько фрагментов оценить")
    ap.add_argument("--out", default="", help="куда сохранить разбор (по умолчанию рядом с образцом)")
    args = ap.parse_args()

    key = os.environ.get("POLZA_API_KEY") or os.environ.get("HERMES_CUSTOM_POLZA_API_KEY")
    if not key:
        print("нет ключа: задайте POLZA_API_KEY или HERMES_CUSTOM_POLZA_API_KEY в окружении")
        return 2

    data = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    items = (data.get("данные") or [])[: args.limit]

    total_rel = confirmed = typed_right = 0
    mismatch: list[tuple] = []
    cost = 0.0
    per_chunk = []
    for n, item in enumerate(items, 1):
        try:
            res = judge_chunk(item, key)
        except urllib.error.HTTPError as e:
            print(f"  ОТКАЗ {e.code} на фрагменте {n}: {e.read().decode('utf-8', 'replace')[:160]}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  ошибка на фрагменте {n}: {type(exc).__name__}: {exc}")
            continue

        answers = res.get("answers") or {}
        usage = res.get("usage") or {}
        cost += float(usage.get("cost_rub") or 0)
        chunk_stat = {"документ": item.get("документ"), "связей": len(item.get("связи") or []),
                      "подтверждено": 0, "тип_верен": 0}
        for i, r in enumerate(item.get("связи") or [], 1):
            total_rel += 1
            есть = answers.get(f"связь_{i}_есть") or {}
            тип = answers.get(f"связь_{i}_тип") or {}
            yes = float(есть.get("noul") or 0)
            if yes >= 0.5:
                confirmed += 1
                chunk_stat["подтверждено"] += 1
            chosen = (тип.get("choice") or "").strip()
            chosen_code = chosen.split(" ")[0] if chosen else ""
            same = chosen_code.upper() == str(r.get("тип") or "").upper()
            if same:
                typed_right += 1
                chunk_stat["тип_верен"] += 1
            else:
                mismatch.append((r.get("от"), r.get("тип"), r.get("к"), chosen or "—", round(yes, 2)))
        per_chunk.append(chunk_stat)
        print(f"  {n:3d}/{len(items)} {str(item.get('документ'))[:34]:36s} "
              f"связей {chunk_stat['связей']:2d}: подтверждено {chunk_stat['подтверждено']:2d}, "
              f"тип верен {chunk_stat['тип_верен']:2d}")

    print("\n" + "=" * 92)
    print("ТОЧНОСТЬ ИЗВЛЕЧЕНИЯ ПО СУДЬЕ JEV")
    print("=" * 92)
    print(f"фрагментов оценено: {len(per_chunk)}; связей проверено: {total_rel}")
    if total_rel:
        print(f"  подтверждено текстом:      {confirmed} ({100 * confirmed / total_rel:.1f}%)")
        print(f"  тип связи совпал с нашим:  {typed_right} ({100 * typed_right / total_rel:.1f}%)")
    print(f"расход: {round(cost, 3)} руб.")
    if mismatch:
        print("\nгде судья поставил другой тип (наши данные → его выбор):")
        from collections import Counter
        cnt = Counter((m[1], m[3]) for m in mismatch)
        for (ours, theirs), n in cnt.most_common(12):
            print(f"  {n:3d}×  наш {ours:16s} → его {theirs}")

    out = args.out or str(Path(args.sample).with_name("jev_graph_accuracy_report.json"))
    Path(out).write_text(json.dumps({"связей": total_rel, "подтверждено": confirmed,
                                     "тип_верен": typed_right, "расход_руб": round(cost, 3),
                                     "по_фрагментам": per_chunk,
                                     "расхождения": mismatch[:200]},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nразбор сохранён: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
