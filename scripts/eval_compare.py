"""Сравнение любых прогонов оценки между собой (общий отчёт).

Запуск:
    python scripts/eval_compare.py reports/eval/rag_eval_ab_off.json reports/eval/rag_eval_ab_cur.json ...
Дополнительно можно указать подписи:
    python scripts/eval_compare.py --labels "выключено,0,03,0,15" a.json b.json c.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

METRICS = ("faithfulness", "answer_relevance", "context_precision", "context_recall")


def load(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--labels", default="")
    ap.add_argument("--collected", default="", help="каталог eval/ для подсчёта фрагментов (по имени файла)")
    args = ap.parse_args()

    labels = [l.strip() for l in args.labels.split(",")] if args.labels else []
    reports = []
    for i, f in enumerate(args.files):
        rep = load(f)
        name = labels[i] if i < len(labels) else Path(f).stem.replace("rag_eval_", "")
        reports.append((name, rep))

    width = max(22, max(len(n) for n, _ in reports) + 2)
    head = f"{'метрика':<{width}}" + "".join(f"{n:>22}" for n, _ in reports)
    print(head)
    print("-" * len(head))
    for m in METRICS:
        line = f"{m:<{width}}"
        for _, rep in reports:
            if not rep:
                line += f"{'НЕТ ОТЧЁТА':>22}"
                continue
            s = rep.get("summary") or {}
            val = s.get(m)
            cov = (s.get("покрытие") or {}).get(m, "")
            line += f"{(f'{val} ({cov})' if val is not None else '—'):>22}"
        print(line)
    for extra in ("вопросов", "стоимость_₽"):
        line = f"{extra:<{width}}"
        for _, rep in reports:
            s = (rep or {}).get("summary") or {}
            line += f"{str(s.get(extra, '—')):>22}"
        print(line)

    # По вопросам: точность контекста (по построчной разметке) — где какой вариант лучше
    print("\n=== ТОЧНОСТЬ КОНТЕКСТА ПО ВОПРОСАМ ===")
    per_q = {}
    for name, rep in reports:
        if not rep:
            continue
        per_q[name] = {r["id"]: r.get("context_precision") for r in rep.get("rows") or []}
    ids = sorted({i for d in per_q.values() for i in d})
    if ids:
        print(f"{'id':<6}" + "".join(f"{n:>22}" for n, _ in reports))
        wins = {n: 0 for n, _ in reports}
        for i in ids:
            vals = {n: per_q.get(n, {}).get(i) for n, _ in reports}
            best = max((v for v in vals.values() if v is not None), default=None)
            line = f"{i:<6}"
            for n, _ in reports:
                v = vals.get(n)
                mark = " *" if (v is not None and best is not None and v == best and
                                list(vals.values()).count(best) == 1) else "  "
                line += f"{str(v):>20}{mark}"
                if mark.endswith("*"):
                    wins[n] += 1
            print(line)
        print("\nпобед по вопросам (единолично лучший): " +
              ", ".join(f"{n} — {w}" for n, w in wins.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
