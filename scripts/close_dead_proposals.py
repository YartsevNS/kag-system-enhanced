"""P0: закрыть мёртвую очередь предложений онтологии (пилот 06.11.2026).

Что произошло: 06.11.2026 один прогон извлечения сложил 1994 предложения в
ontology_proposals. Канал, который их создавал, в репозитории больше не
существует (писателя в таблицу нет — только чтение через proposal_service),
а 1265 записей остались в статусе «proposed». Для заказчика это выглядит как
«система выдала 1265 задач на ручную проверку», хотя проверить их некому и
незачем: факты за это время писались в TypeDB напрямую (typedb_writer).

Скрипт переводит такие записи в superseded с объяснением в review_note.
Ничего не удаляется: история остаётся, решение видно.

Запуск (внутри контейнера или с доступом к KAG_DB_URL):
    python scripts/close_dead_proposals.py --dry-run
    python scripts/close_dead_proposals.py --before 2026-11-07
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="только показать")
    ap.add_argument("--before", default="2026-11-07",
                    help="закрыть предложения, созданные до этой даты (YYYY-MM-DD)")
    ap.add_argument("--note", default="очередь пилота 06.11.2026: писателя в таблицу нет, "
                                     "факты пишутся в TypeDB напрямую (typedb_writer)")
    args = ap.parse_args()

    url = os.environ.get("KAG_DB_URL", "")
    if not url:
        print("KAG_DB_URL не задан", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine, text

    eng = create_engine(url, future=True, pool_pre_ping=True)
    with eng.begin() as conn:
        rows = conn.execute(text(
            "SELECT status, count(*) FROM ontology_proposals GROUP BY 1 ORDER BY 1"
        )).all()
        print("было:", ", ".join(f"{s}={n}" for s, n in rows))
        dead = conn.execute(text(
            "SELECT count(*) FROM ontology_proposals "
            "WHERE status = 'proposed' AND created_at < cast(:d as timestamptz)"
        ), {"d": args.before}).scalar()
        print(f"к закрытию (proposed, created_at < {args.before}): {dead}")
        if args.dry_run or not dead:
            return 0
        res = conn.execute(text(
            "UPDATE ontology_proposals SET status = 'superseded', reviewer = 'autopilot', "
            "reviewed_at = now(), review_note = :note "
            "WHERE status = 'proposed' AND created_at < cast(:d as timestamptz)"
        ), {"note": args.note, "d": args.before})
        print(f"переведено в superseded: {res.rowcount}")

    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT status, count(*) FROM ontology_proposals GROUP BY 1 ORDER BY 1"
        )).all()
        print("стало:", ", ".join(f"{s}={n}" for s, n in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
