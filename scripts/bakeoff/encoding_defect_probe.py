"""Разбор дефекта кодировки: где испорчено, целы ли исходники, лечится ли восстановлением.

Отвечает на три вопроса, от которых зависит лечение:
  1) какие документы задеты и сколько в них битых фрагментов;
  2) целы ли ИСХОДНЫЕ файлы (если да — виноват конвейер, а не данные);
  3) восстанавливается ли текст из базы обратным преобразованием (тогда переливка не нужна)
     или потери необратимы (тогда только переливка из исходников).

Запуск на стенде:
    docker exec kag-api python /app/data/encoding_defect_probe.py
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re

MOJI = ('Ð°', 'Ð¾', 'Ñ€', 'Ñ‚', 'Ðµ', 'Ð¸')


def cyr_share(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if re.match(r"[а-яА-ЯёЁ]", c)) / len(letters)


def is_mangled(text: str) -> bool:
    t = text or ""
    if len(t) < 60:
        return False
    return sum(t.count(m) for m in MOJI) >= 5 and cyr_share(t) < 0.4


def try_repair(text: str) -> tuple[str, bool]:
    """Обратное преобразование «UTF-8, прочитанный как cp1252/latin-1».

    Возвращает (текст, удалось_ли_без_потерь). Без потерь возможно не всегда: если на каком-то
    шаге уже стоял errors='replace', часть символов потеряна навсегда — в тексте есть заменители.
    """
    for enc, strict in (("cp1252", True), ("latin-1", True), ("latin-1", False)):
        try:
            fixed = text.encode(enc, errors="strict" if strict else "ignore").decode("utf-8")
            if cyr_share(fixed) > 0.7:
                return fixed, strict
        except Exception:
            continue
    return text, False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--inbox", default="/app/data/inbox")
    args = ap.parse_args()

    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
    )

    with drv.session() as s:
        docs = list(s.run(
            """
            MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
            RETURN d.id AS id, d.filename AS fn, count(c) AS total,
                   count(CASE WHEN c.text CONTAINS $a THEN 1 END) AS bad
            ORDER BY bad DESC
            """,
            a="Ð°",
        ))

    affected = [d for d in docs if d["bad"] > 0]
    print("=" * 96)
    print("РАЗБОР ДЕФЕКТА КОДИРОВКИ")
    print("=" * 96)
    print(f"документов всего: {len(docs)}, из них с испорченным текстом: {len(affected)}")
    print()
    print(f"{'файл':30s} {'битых/всего':>13s} {'исходник':>10s} {'кириллица в исходнике':>22s} {'восстановимо из базы':>21s}")

    source_ok = source_missing = []
    repair_ok = repair_bad = 0
    for d in affected:
        fn = d["fn"] or ""
        path = pathlib.Path(args.inbox) / fn
        exists = path.exists()
        src_share = None
        if exists:
            try:
                src = path.read_text(encoding="utf-8")
                src_share = cyr_share(src[:20000])
            except Exception:
                exists = "не utf-8"
        if exists is True and (src_share or 0) > 0.5:
            source_ok.append(fn)
        else:
            source_missing.append(fn)

        with drv.session() as s:
            texts = [r["t"] or "" for r in s.run(
                "MATCH (d:Document {id: $i})-[:HAS_CHUNK]->(c:Chunk) RETURN c.text AS t LIMIT $n",
                i=d["id"], n=args.samples,
            )]
        good = sum(1 for t in texts if cyr_share(try_repair(t)[0]) > 0.7)
        repair_ok += good
        repair_bad += len(texts) - good
        src_txt = f"{src_share:.2f}" if isinstance(src_share, float) else str(exists)
        print(f"{fn[:30]:30s} {d['bad']:6d}/{d['total']:<6d} {str(exists):>10s} {src_txt:>22s} {f'{good}/{len(texts)}':>21s}")

    print()
    print(f"исходники в порядке: {len(source_ok)} из {len(affected)}")
    if source_missing:
        print(f"  нет/не читаются: {source_missing[:8]}")
    print(f"восстановление из базы: удалось {repair_ok} из {repair_ok + repair_bad} проверенных фрагментов")
    verdict = ("текст можно починить на месте" if repair_bad == 0
               else "потери необратимы — нужна переливка из исходников")
    print(f"вывод: {verdict}")

    drv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
