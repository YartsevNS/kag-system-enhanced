"""Проверка: ONNX-сервис реранка даёт ТОТ ЖЕ порядок, что torch-модель.

Зачем: ONNX в 12 раз быстрее (120 мс против 1,46 с на 10 парах), но ускорение имеет смысл только
если качество не изменилось. Динамическое int8-квантование, например, порядок сломало — поэтому
перед подключением сервиса к api сравниваем порядок фрагментов с эталонным (torch) на всех 14 вопросах.

Запуск на сервере моделей (сервис уже поднят):
    ~/kag-eval/venv/bin/python scripts/rerank_onnx_check.py
"""
from __future__ import annotations

import json
import statistics
import time
import urllib.request
from pathlib import Path

CAND = Path.home() / "kag-eval/eval/collected_ab_off.json"
REF = Path.home() / "kag-eval/eval/rerank_DiTy_cross-encoder-russian-msmarco.json"
URL = "http://localhost:8010/rerank"


def post(payload: dict) -> dict:
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def main() -> int:
    collected = json.loads(CAND.read_text(encoding="utf-8"))
    ref = {r["id"]: r for r in json.loads(REF.read_text(encoding="utf-8"))} if REF.exists() else {}

    same_top1 = same_top5 = 0
    times = []
    diffs = []
    for row in collected:
        cands = [{"id": c.get("id") or f"c{i}", "text": (c.get("content") or "")[:2000]}
                 for i, c in enumerate(row.get("contexts") or [])]
        if not cands:
            continue
        t0 = time.time()
        res = post({"query": row["question"], "candidates": cands, "top_k": 5})
        times.append((time.time() - t0) * 1000)
        got = [s["id"] for s in res["scores"]]

        r = ref.get(row["id"])
        if r:
            # эталонный порядок из torch-прогона: сортируем context по rank_after
            exp_ctx = sorted(r.get("contexts") or [], key=lambda c: c.get("rank_after") or 99)
            exp_ids = []
            for c in exp_ctx:
                # в собранных контекстах id может быть None — сопоставляем по первым 80 символам
                key = (c.get("content") or "")[:80]
                m = [cc["id"] for cc in cands if (cc["text"] or "")[:80] == key]
                exp_ids.extend(m[:1])
            if exp_ids:
                same_top1 += int(got[:1] == exp_ids[:1])
                same_top5 += int(got == exp_ids[:5] or got[:len(exp_ids)] == exp_ids)
                diffs.append((row["id"], got[:3], exp_ids[:3]))

    n = len(times)
    print("=== ONNX-сервис реранка против torch ===")
    print(f"  вопросов проверено: {n}")
    print(f"  время на запрос: медиана {statistics.median(times):.0f} мс, "
          f"максимум {max(times):.0f} мс (torch на 10 парах: ~1460 мс)")
    if diffs:
        print(f"  топ-1 совпал: {same_top1}/{len(diffs)} | весь топ-5 совпал: {same_top5}/{len(diffs)}")
        bad = [d for d in diffs if d[1] != d[2]]
        if bad:
            print("  расхождения (onnx | torch):")
            for pid, a, b in bad[:5]:
                print(f"    {pid}: {a} | {b}")
        else:
            print("  расхождений нет: порядок совпадает с torch на всём наборе")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
