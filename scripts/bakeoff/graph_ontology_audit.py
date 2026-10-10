"""Аудит онтологии графа: проверка существующих связей по схеме «тип — связь — тип».

Зачем: в графе 135 тысяч связей, за которые уже заплачено токенами, но их качество ни разу не
измерялось. Промпт извлечения ограничивает ТИПЫ СУЩНОСТЕЙ, а типы связей не ограничивает вовсе —
поэтому в графе встречаются пары вида «юридический термин — датирован — дата», и никто этого не видит.

Прибор ничего не меняет: только считает. Проверяются смысловые связи между сущностями; системные
(упоминания, фрагменты, разделы) пропускаются — их пишет код, а не модель.

Запуск на стенде (там есть драйвер и доступ к базе):
    docker exec kag-api python /app/data/graph_ontology_audit.py
    docker exec kag-api python /app/data/graph_ontology_audit.py --json /app/data/graph_ontology_audit.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict

# Что модель вообще не должна была порождать сама — эти связи пишет код.
STRUCTURAL = {"MENTIONS", "HAS_CHUNK", "SECTION_CHUNK", "HAS_SECTION"}

# Проверка идёт по ЕДИНОЙ онтологии (src/indexing/graph_ontology.py), а не по отдельной таблице:
# раньше здесь лежал свой список, он отстал от онтологии, и типы вроде PART_OF/ISSUED_BY считались
# «не описанными схемой» — то есть аудит показывал нарушения там, где всё было верно.
try:
    from graph_ontology import SEMANTIC_CODES, allowed_pairs, normalize_relation  # type: ignore
except Exception:  # когда прибор лежит рядом с модулем

    SEMANTIC_CODES = {"RELATED_TO"}  # type: ignore

    def normalize_relation(raw: str) -> str:  # type: ignore
        return (raw or "").strip().upper()

    def allowed_pairs(code: str):  # type: ignore
        return None


ALLOWED_PAIRS: dict[str, set[tuple[str, str]] | None] = {}


def fetch_pairs(session) -> list[dict]:
    q = """
    MATCH (a:Entity)-[r]->(b:Entity)
    RETURN type(r) AS rel,
           coalesce(a.type, '(без типа)') AS t_a,
           coalesce(b.type, '(без типа)') AS t_b,
           coalesce(r.extractor_version, '(нет)') AS ver,
           count(*) AS n
    ORDER BY n DESC
    """
    return [dict(rec) for rec in session.run(q)]


def fetch_structural(session) -> list[dict]:
    q = """
    MATCH (a)-[r]->(b)
    WHERE type(r) IN $types
    RETURN type(r) AS rel, labels(a)[0] AS t_a, labels(b)[0] AS t_b, count(*) AS n
    ORDER BY n DESC
    """
    return [dict(rec) for rec in session.run(q, types=sorted(STRUCTURAL))]


def fetch_duplicates(session) -> int:
    """Сколько связей одного типа соединяют одну и ту же пару узлов больше одного раза."""
    q = """
    MATCH (a:Entity)-[r]->(b:Entity)
    WITH a, b, type(r) AS t, count(*) AS c
    WHERE c > 1
    RETURN coalesce(sum(c - 1), 0) AS лишних
    """
    rec = session.run(q).single()
    return int(rec["лишних"]) if rec else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="куда сохранить машинный отчёт")
    args = ap.parse_args()

    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    if not password:
        print("нет NEO4J_PASSWORD в окружении")
        return 2

    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        pairs = fetch_pairs(session)
        structural = fetch_structural(session)
        dupes = fetch_duplicates(session)
    driver.close()

    total_sem = sum(r["n"] for r in pairs)
    total_str = sum(r["n"] for r in structural)

    ok = viol = unchecked = 0
    by_rel: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "viol": 0, "unchecked": 0})
    viol_rows: list[dict] = []
    belongs_rows: list[dict] = []

    for r in pairs:
        rel, t_a, t_b, n = r["rel"], r["t_a"], r["t_b"], r["n"]
        code = normalize_relation(rel)
        if code not in SEMANTIC_CODES:
            # Тип связи не из онтологии: это нарушение (модель выдумала тип), а не «не проверялось».
            viol += n
            by_rel[rel]["viol"] += n
            viol_rows.append({**r, "почему": "тип связи не из онтологии"})
            continue
        allowed = allowed_pairs(code)          # None — пары для этого типа не заданы (RELATED_TO)
        if allowed is None:
            unchecked += n
            by_rel[rel]["unchecked"] += n
            continue
        if (t_a, t_b) in allowed:
            ok += n
            by_rel[rel]["ok"] += n
        else:
            viol += n
            by_rel[rel]["viol"] += n
            viol_rows.append({**r, "почему": "пара типов не допускается онтологией"})

    print("=" * 78)
    print("АУДИТ ОНТОЛОГИИ ГРАФА (проверка пар «тип сущности — связь — тип сущности»)")
    print("=" * 78)
    print(f"связей всего:            {total_sem + total_str}")
    print(f"  системные (код):       {total_str}")
    print(f"  смысловые (от модели): {total_sem}")
    print()
    print(f"  проходят проверку:     {ok} ({100 * ok / max(total_sem, 1):.1f}%)")
    print(f"  НАРУШАЮТ схему:        {viol} ({100 * viol / max(total_sem, 1):.1f}%)")
    print(f"  не проверялись:        {unchecked} ({100 * unchecked / max(total_sem, 1):.1f}%) — тип без заданной семантики")
    print(f"  дублирующихся связей:  {dupes}")

    print("\n--- по типам связей (проходит / нарушает / не проверяется) ---")
    for rel, c in sorted(by_rel.items(), key=lambda kv: -(kv[1]["ok"] + kv[1]["viol"] + kv[1]["unchecked"])):
        tot = c["ok"] + c["viol"] + c["unchecked"]
        flag = "МУСОРКА (тип не определён)" if rel == "RELATED_TO" else ""
        print(f"  {rel:16s} всего {tot:6d} | ок {c['ok']:6d} | наруш {c['viol']:6d} | без проверки {c['unchecked']:6d}  {flag}")

    if viol_rows:
        print("\n--- крупнейшие нарушения (топ-15) ---")
        for r in sorted(viol_rows, key=lambda x: -x["n"])[:15]:
            print(f"  {r['rel']:14s} {r['t_a']:14s} -> {r['t_b']:14s} {r['n']:6d}  ({r['почему']})")

    if belongs_rows:
        print("\n--- тип BELONGS_TO: какие пары он на самом деле покрывает (топ-12) ---")
        for r in sorted(belongs_rows, key=lambda x: -x["n"])[:12]:
            print(f"  {r['t_a']:14s} -> {r['t_b']:14s} {r['n']:6d}")

    from collections import Counter
    vers = Counter()
    for r in pairs:
        vers[r["ver"]] += r["n"]
    print("\n--- по версии извлечения (кто это построил) ---")
    for v, n in vers.most_common(6):
        print(f"  {v:10s} {n:8d}")

    if args.json:
        payload = {
            "всего_связей": total_sem + total_str,
            "системных": total_str,
            "смысловых": total_sem,
            "проходят": ok,
            "нарушают": viol,
            "не_проверялись": unchecked,
            "дубли": dupes,
            "по_типам": {k: dict(v) for k, v in by_rel.items()},
            "нарушения": sorted(viol_rows, key=lambda x: -x["n"])[:60],
            "belongs_to_пары": sorted(belongs_rows, key=lambda x: -x["n"])[:40],
            "по_версиям": dict(vers),
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nмашинный отчёт: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
