"""Сводка A/B/C по отбору фрагментов: собирает три отчёта судьи и данные прогонов в одну таблицу.

Запуск (после scripts/eval_rag_judge.py по каждому варианту):
    python scripts/eval_ab_report.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = [("off", "выключено (0)"), ("cur", "текущее (0,03)"), ("agg", "жёсткое (0,15)")]
METRICS = ("faithfulness", "answer_relevance", "context_precision", "context_recall")


def load_report(variant: str) -> dict | None:
    p = ROOT / "reports" / "eval" / f"rag_eval_ab_{variant}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def load_collected(variant: str) -> list | None:
    p = ROOT / "eval" / f"collected_ab_{variant}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def avg(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def main() -> int:
    print("=== A/B/C по отбору фрагментов (min_score_gap) ===\n")
    header = f"{'метрика':<20}" + "".join(f"{lab:>20}" for _, lab in VARIANTS)
    print(header)
    print("-" * len(header))

    data = {}
    for v, _ in VARIANTS:
        rep = load_report(v)
        col = load_collected(v)
        if not rep or not col:
            data[v] = None
            continue
        data[v] = {
            "summary": rep["summary"],
            "frag_avg": avg([c.get("fragments_in_prompt") for c in col]),
            "answer_avg": avg([len(c.get("answer") or "") for c in col]),
            "ctx_avg": avg([len(c.get("contexts") or []) for c in col]),
        }

    for m in METRICS:
        line = f"{m:<20}"
        for v, _ in VARIANTS:
            s = (data.get(v) or {}).get("summary") or {}
            val = s.get(m)
            cov = (s.get("покрытие") or {}).get(m, "")
            line += f"{f'{val} ({cov})' if val is not None else '—':>20}"
        print(line)

    for key, label in (("frag_avg", "фрагментов в промпте"), ("ctx_avg", "контекстов найдено"),
                       ("answer_avg", "длина ответа, симв.")):
        line = f"{label:<20}"
        for v, _ in VARIANTS:
            val = (data.get(v) or {}).get(key)
            line += f"{val if val is not None else '—':>20}"
        print(line)

    line = f"{'стоимость судьи, ₽':<20}"
    for v, _ in VARIANTS:
        s = (data.get(v) or {}).get("summary") or {}
        line += f"{s.get('стоимость_₽', '—'):>20}"
    print(line)

    # Сравнение по вопросам: где вариант улучшил/ухудшил точность контекста
    print("\n=== ПО ВОПРОСАМ: точность контекста (precision) ===")
    rows = {}
    for v, _ in VARIANTS:
        rep = load_report(v)
        if not rep:
            continue
        rows[v] = {r["id"]: r.get("context_precision") for r in rep["rows"]}
    ids = sorted({i for r in rows.values() for i in r})
    print(f"{'id':<6}" + "".join(f"{lab:>20}" for _, lab in VARIANTS))
    for i in ids:
        line = f"{i:<6}"
        for v, _ in VARIANTS:
            line += f"{rows.get(v, {}).get(i, '—'):>20}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
