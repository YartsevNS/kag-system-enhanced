"""Spike: помогает ли НАСТОЯЩИЙ русский cross-encoder, и сколько это стоит по времени.

Зачем spike, а не сразу правка кода: реранкер — это лишние секунды в каждом вопросе и новая
модель в образе. Сначала проверяем пользу на уже собранных кандидатах (топ-10 векторного поиска
по 14 вопросам), и только если польза есть — тащим модель в api.

Что делает:
  1. берёт кандидатов из eval/collected_ab_off.json (то, что нашёл обычный поиск);
  2. пересортировывает их cross-encoder'ом (запрос, фрагмент) -> оценка релевантности;
  3. оставляет топ-5 — и формирует ДВА файла для сравнения судьёй:
       rerank_<модель>.json  — топ-5 после реранка
       dense_top5.json       — то же число фрагментов, но в порядке векторного score;
  4. считает: как часто менялся лучший фрагмент, сколько занимает реранк на один вопрос (CPU).

Запуск:
    C:/VSCODE_PROJECT/.venv-rerank/Scripts/python.exe scripts/rerank_spike.py --model DiTy/cross-encoder-russian-msmarco
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collected", default=str(ROOT / "eval" / "collected_ab_off.json"))
    ap.add_argument("--model", default="DiTy/cross-encoder-russian-msmarco")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--max-length", type=int, default=512)
    args = ap.parse_args()

    from sentence_transformers import CrossEncoder

    print(f"модель: {args.model}", flush=True)
    t0 = time.time()
    model = CrossEncoder(args.model, max_length=args.max_length)
    print(f"  загрузка модели: {time.time() - t0:.1f} с", flush=True)

    data = json.loads(Path(args.collected).read_text(encoding="utf-8"))
    reranked_rows, dense_rows = [], []
    latencies, top1_changed, scores_gap = [], 0, []

    for row in data:
        ctx = row.get("contexts") or []
        if not ctx:
            continue
        q = row["question"]
        pairs = [(q, (c.get("content") or "")[:2000]) for c in ctx]
        t1 = time.time()
        scores = model.predict(pairs, batch_size=8, show_progress_bar=False)
        dt = time.time() - t1
        latencies.append(dt)

        order = sorted(range(len(ctx)), key=lambda i: float(scores[i]), reverse=True)
        reranked = []
        for pos, i in enumerate(order[:args.top_k], 1):
            c = dict(ctx[i])
            c["rerank_score"] = round(float(scores[i]), 4)
            c["dense_score"] = c.get("score")
            c["rank_after"] = pos
            c["rank_before"] = i + 1
            reranked.append(c)

        dense = []
        for pos, c in enumerate(ctx[:args.top_k], 1):
            c = dict(c)
            c["rank_after"] = pos
            c["rank_before"] = pos
            dense.append(c)

        if (order[0] + 1) != 1:
            top1_changed += 1
        s = sorted((float(x) for x in scores), reverse=True)
        if len(s) > 1 and s[0]:
            scores_gap.append((s[0] - s[1]) / s[0])

        reranked_rows.append({"id": row["id"], "question": q, "answer": row.get("answer") or "",
                              "contexts": reranked})
        dense_rows.append({"id": row["id"], "question": q, "answer": row.get("answer") or "",
                           "contexts": dense})

    safe = args.model.replace("/", "_")
    out_r = ROOT / "eval" / f"rerank_{safe}.json"
    out_d = ROOT / "eval" / "dense_top5.json"
    out_r.write_text(json.dumps(reranked_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    out_d.write_text(json.dumps(dense_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== SPIKE: {args.model} ===")
    print(f"  вопросов: {len(reranked_rows)} | топ-K: {args.top_k}")
    print(f"  лучший фрагмент сместился в {top1_changed} из {len(reranked_rows)} вопросов")
    if latencies:
        print(f"  время реранка на вопрос: медиана {statistics.median(latencies):.2f} с, "
              f"максимум {max(latencies):.2f} с (CPU, 10 кандидатов)")
    if scores_gap:
        print(f"  отрыв лучшего от второго по реранку: медиана {statistics.median(scores_gap):.1%} "
              f"(по векторному score отрыв был 1-6%)")
    print(f"  сохранено: {out_r.name} и {out_d.name}")
    print("  дальше: оценить оба файла судьёй и сравнить context_precision")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
