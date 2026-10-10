"""Свод по доменам и фасетам: как фильтр ведёт себя на кибербезе и юристах.

Читает отчёт замера (theme_filter_ab.json) и раскладывает метрики по домену вопроса, а также
показывает распределение фасетов в базе. Отвечает на два вопроса владельца:
  1) не теряет ли фильтр по теме документы-эталоны в этих двух предметных областях;
  2) что вообще размечено в фасетах (предмет защиты, нормативность) и есть ли пустые.

Запуск в контейнере api:
    docker exec kag-api python /app/data/facet_domain_report.py
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

REPORT = "/app/data/theme_filter_ab.json"


def load_report() -> dict:
    with open(REPORT, encoding="utf-8") as fh:
        return json.load(fh)


def metrics(rows: list[dict], key: str) -> dict:
    """hit@1 / hit@3 / MRR / ненайденные по одному способу фильтрации.

    В отчёте лежат РАНГИ: `legacy`, `rubric_ранг`, `без_фильтра` (None — эталон не найден).
    """
    h1 = h3 = missing = n = 0
    rr = 0.0
    for r in rows:
        if key not in r:
            continue
        n += 1
        rank = r.get(key)
        if rank is None:
            missing += 1
            continue
        if rank == 1:
            h1 += 1
        if rank <= 3:
            h3 += 1
        rr += 1.0 / rank
    return {"вопросов": n, "hit@1": round(h1 / max(n, 1), 3), "hit@3": round(h3 / max(n, 1), 3),
            "MRR": round(rr / max(n, 1), 3), "не найдено": missing}


def facets_from_db() -> dict:
    rows = []
    try:
        import psycopg2

        conn = psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "kag-postgres"),
            dbname=os.environ.get("POSTGRES_DB", "kag"),
            user=os.environ.get("POSTGRES_USER", "kag"),
            password=os.environ.get("POSTGRES_PASSWORD", ""))
        with conn.cursor() as cur:
            cur.execute("select document_type, rubrics, facets from documents")
            rows = cur.fetchall()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        return {"ошибка чтения базы": f"{type(exc).__name__}: {exc}"}

    rub = Counter()
    prot = Counter()
    norm = Counter()
    empty_rub = empty_fac = 0
    for _dt, rubrics, facets in rows:
        vals = rubrics if isinstance(rubrics, (list, dict)) else []
        if isinstance(vals, dict):
            vals = list(vals.keys())
        if not vals:
            empty_rub += 1
        for v in vals:
            rub[str(v)] += 1
        if not facets:
            empty_fac += 1
            continue
        for key, counter in (("protection_subject", prot), ("normative_force", norm)):
            raw = facets.get(key) if isinstance(facets, dict) else None
            values = raw if isinstance(raw, list) else ([raw] if raw else [])
            for v in values:
                counter[str(v)] += 1
    return {"всего документов": len(rows), "без темы": empty_rub, "без фасетов": empty_fac,
            "темы": rub.most_common(12), "предмет защиты": prot.most_common(10),
            "нормативность": norm.most_common(8)}


def main() -> int:
    rep = load_report()
    rows = rep.get("per_question") or rep.get("вопросы") or []
    if not rows:
        print("в отчёте нет построчных результатов:", list(rep.keys())[:8])
        return 1

    by_domain: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_domain[str(r.get("domain") or "не определён")].append(r)

    variants = ["без_фильтра", "legacy", "rubric_ранг"]

    print("=" * 88)
    print("ФИЛЬТР ТЕМЫ И ФАСЕТЫ ПО ДОМЕНАМ")
    print("=" * 88)
    print(f"вопросов в замере: {len(rows)}; способы: {', '.join(variants)}")
    for dom, sub in sorted(by_domain.items(), key=lambda kv: -len(kv[1])):
        print(f"\nдомен «{dom}» — вопросов {len(sub)}")
        for v in variants:
            m = metrics(sub, v)
            if m["вопросов"]:
                print(f"  {v:14s} hit@1 {m['hit@1']:.3f}  hit@3 {m['hit@3']:.3f}  "
                      f"MRR {m['MRR']:.3f}  не найдено {m['не найдено']}")

    print("\n" + "=" * 88)
    print("ЧТО РАЗМЕЧЕНО (фасеты и темы)")
    print("=" * 88)
    for k, v in facets_from_db().items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
