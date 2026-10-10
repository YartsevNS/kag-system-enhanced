"""Время обработки документов: от создания записи до последнего обновления, по этапам из журнала.

Зачем: замер «сколько идёт обработка» нужен для планирования (сколько документов в час, во сколько
обойдётся корпус). Берём фактическое время из реестра (created_at → updated_at) и разбивку этапов,
если она есть в журнале обработки документа.

Запуск внутри контейнера api:  docker exec kag-api python /app/data/processing_times.py [--limit 8]
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    from sqlalchemy import text

    from src.database.session import get_session_local

    sess = get_session_local()()
    rows = sess.execute(text(
        "select id, filename, status, chunks_count, created_at, updated_at, "
        "extract(epoch from (updated_at - created_at)) as seconds "
        "from documents where chunks_count > 0 order by created_at desc limit :n"),
        {"n": args.limit}).fetchall()
    print(f"{'документ':<44} {'фрагм':>6} {'секунд':>8}")
    for did, name, status, chunks, created, updated, seconds in rows:
        secs = f"{float(seconds):.0f}" if seconds is not None else "—"
        print(f"{str(name)[:42]:<44} {chunks:>6} {secs:>8}")
    print("\n(время включает всё: распознавание, чанкинг, векторы, таблицы и, если включён, граф;"
          "\n оно снято от создания записи до последнего обновления, поэтому включает и ожидание в очереди)")
    sess.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
