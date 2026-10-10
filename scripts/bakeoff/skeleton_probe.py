"""Проверка слоя правил на живом корпусе: что находится без модели и без токенов.

Читает текст фрагментов прямо из графа (свойство text узла Chunk), прогоняет детерминированные
извлекатели и показывает: покрытие (в скольких фрагментах что-то найдено), сколько разных значений,
и образцы для ручной проверки точности. Ничего не пишет в базу — только считает.

Запуск на стенде:
    docker cp src/indexing/deterministic_extractors.py kag-api:/app/data/
    docker cp scripts/bakeoff/skeleton_probe.py kag-api:/app/data/
    docker exec kag-api python /app/data/skeleton_probe.py --limit 400 --samples 15
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, "/app/data")
from deterministic_extractors import EXTRACTOR_VERSION, extract  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400, help="сколько фрагментов разобрать")
    ap.add_argument("--samples", type=int, default=15, help="сколько примеров показать по каждому виду")
    ap.add_argument("--json", default="", help="куда положить машинный отчёт")
    args = ap.parse_args()

    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
    password = os.environ.get("NEO4J_PASSWORD", "")
    if not password:
        print("нет NEO4J_PASSWORD")
        return 2

    driver = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), password))
    with driver.session() as session:
        total = session.run("MATCH (c:Chunk) RETURN count(c) AS n").single()["n"]
        # Случайная выборка по всему корпусу: брать «первые по id» нельзя — так попадает один
        # документ и выводы получаются смещёнными (проверено: первые 400 фрагментов дали всего
        # 8 разных номеров стандартов, потому что все были из одной серии ГОСТов).
        rows = list(session.run(
            """
            MATCH (c:Chunk)
            WHERE c.text IS NOT NULL AND size(c.text) > 0
            WITH c, rand() AS r ORDER BY r LIMIT $lim
            RETURN c.id AS id, c.text AS text
            """,
            lim=args.limit,
        ))
    driver.close()

    values: dict[str, Counter] = {k: Counter() for k in
                                  ("gost", "sp", "fz", "doc_numbers", "clauses", "dates")}
    coverage = Counter()
    chunk_hits = 0
    lengths = []

    for r in rows:
        sk = extract(r["text"])
        lengths.append(len(r["text"] or ""))
        if not sk.is_empty():
            chunk_hits += 1
        for name in values:
            got = getattr(sk, name)
            if got:
                coverage[name] += 1
                values[name].update(got)

    print("=" * 78)
    print(f"СЛОЙ ПРАВИЛ НА ЖИВОМ КОРПУСЕ (извлекатель {EXTRACTOR_VERSION})")
    print("=" * 78)
    print(f"фрагментов в базе: {total}, разобрано: {len(rows)}")
    if lengths:
        print(f"длина фрагмента: средняя {sum(lengths)//len(lengths)} знаков, "
              f"от {min(lengths)} до {max(lengths)}")
    print(f"фрагментов хотя бы с одной находкой: {chunk_hits} "
          f"({100 * chunk_hits / max(len(rows), 1):.0f}%)")
    print()
    names = {"gost": "НОМЕРА ГОСТ/СТАНДАРТОВ", "sp": "СП/СНиП/СанПиН", "fz": "НОМЕРА ЗАКОНОВ",
             "doc_numbers": "НОМЕРА ДОКУМЕНТОВ", "clauses": "ССЫЛКИ НА ПУНКТЫ", "dates": "ДАТЫ"}
    for key, title in names.items():
        print(f"{title:26s} фрагментов {coverage[key]:4d} | разных значений {len(values[key]):5d}")
    print()

    for key, title in names.items():
        top = values[key].most_common(args.samples)
        if not top:
            continue
        print(f"--- {title}: образцы (значение × сколько раз встретилось) ---")
        for v, n in top:
            print(f"    {v}  ×{n}")
        print()

    if args.json:
        payload = {
            "извлекатель": EXTRACTOR_VERSION,
            "фрагментов_в_базе": int(total),
            "разобрано_фрагментов": len(rows),
            "с_находками": chunk_hits,
            "покрытие_по_видам": dict(coverage),
            "разных_значений": {k: len(v) for k, v in values.items()},
            "топ": {k: v.most_common(50) for k, v in values.items()},
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"машинный отчёт: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
