"""Проверка ONNX против torch БЕЗ изменения сервиса: считаем напрямую, сравниваем порядок.

Зачем отдельный скрипт: перезапуск сервиса — действие со сменой состояния (нужно подтверждение), а
проверить качество можно и без него: берём те же 14 вопросов и те же 10 кандидатов, считаем ONNX
напрямую и сравниваем порядок с эталоном, полученным через torch (sentence-transformers).

Запуск на сервере моделей:
    ~/kag-eval/venv/bin/python scripts/rerank_onnx_check_local.py
"""
from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

DIR = Path(os.path.expanduser("~/kag-eval/models/DiTy_cross-encoder-russian-msmarco-onnx"))
COLLECTED = Path(os.path.expanduser("~/kag-eval/eval/collected_ab_off.json"))
REF = Path(os.path.expanduser("~/kag-eval/eval/rerank_DiTy_cross-encoder-russian-msmarco.json"))
THREADS = int(os.environ.get("RERANK_THREADS", "16"))


def main() -> int:
    import numpy as np
    import onnxruntime as ort
    from transformers import AutoTokenizer

    so = ort.SessionOptions()
    so.intra_op_num_threads = THREADS
    sess = ort.InferenceSession(str(DIR / "model.onnx"), so, providers=["CPUExecutionProvider"])
    tok = AutoTokenizer.from_pretrained(DIR)
    names = [i.name for i in sess.get_inputs()]

    collected = json.loads(COLLECTED.read_text(encoding="utf-8"))
    ref = {r["id"]: r for r in json.loads(REF.read_text(encoding="utf-8"))} if REF.exists() else {}

    times, rows = [], []
    for row in collected:
        ctx = row.get("contexts") or []
        if not ctx:
            continue
        texts = [(c.get("content") or "")[:2000] for c in ctx]
        enc = tok([row["question"]] * len(texts), texts, padding=True, truncation=True,
                  max_length=512, return_tensors="np")
        feed = {k: v for k, v in enc.items() if k in names}
        t0 = time.time()
        out = sess.run(None, feed)
        times.append((time.time() - t0) * 1000)
        sc = [float(x) for x in np.array(out[0]).reshape(-1)]
        order = sorted(range(len(sc)), key=lambda i: -sc[i])

        # эталон: порядок из torch-прогона (rank_after), сопоставляем по первым 80 символам
        exp_ids = []
        r = ref.get(row["id"])
        if r:
            for c in sorted(r.get("contexts") or [], key=lambda c: c.get("rank_after") or 99):
                key = (c.get("content") or "")[:80]
                for i, t in enumerate(texts):
                    if t[:80] == key:
                        exp_ids.append(i)
                        break
        got = order[:5]
        rows.append((row["id"], got, exp_ids[:5]))

    print("=== ONNX напрямую против torch (без изменения сервиса) ===")
    print(f"  вопросов: {len(rows)}")
    print(f"  время на вопрос: медиана {statistics.median(times):.0f} мс, "
          f"максимум {max(times):.0f} мс (torch: ~1460 мс, ускорение "
          f"{1460/statistics.median(times):.1f}x)")
    pairs = [(g, e) for _, g, e in rows if e]
    if pairs:
        top1 = sum(1 for g, e in pairs if g[:1] == e[:1])
        top5 = sum(1 for g, e in pairs if g == e)
        print(f"  топ-1 совпал с torch: {top1}/{len(pairs)} | весь топ-5: {top5}/{len(pairs)}")
        bad = [(pid, g, e) for pid, g, e in rows if e and g != e]
        for pid, g, e in bad[:4]:
            print(f"    {pid}: onnx {g} | torch {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
