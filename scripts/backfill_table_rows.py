"""Заполнить строчный слой (table_rows) по уже сохранённым таблицам документов.

Зачем: этап 2 добавил строчный слой, но у документов, обработанных раньше, строки не
записаны — вычисления по SQL на них работать не будут. Скрипт проходит по `document_tables`
и раскладывает сохранённые строки в `table_rows`, попутно дозаполняя `table_id`,
`row_count` и `quality` там, где их нет.

Идемпотентен: повторный запуск перезаписывает строки той же таблицы (не удваивает).
Переиндексация документа позже пересоберёт и table_id, и строки в том же виде.

Запуск (внутри контейнера api, где есть доступ к БД):
    docker exec kag-api python /app/scripts/backfill_table_rows.py --dry-run
    docker exec kag-api python /app/scripts/backfill_table_rows.py
    docker exec kag-api python /app/scripts/backfill_table_rows.py --document-id <id>

ВНИМАНИЕ: в контейнер копируется только `src/`, поэтому файл кладётся в bind-каталог
`./data` (виден как /app/data) — запускать как `python /app/data/backfill_table_rows.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402

from src.database.document_table_models import DocumentTable  # noqa: E402
from src.database.session import get_session_local  # noqa: E402
from src.indexing.table_ids import make_table_id, table_quality  # noqa: E402
from src.indexing.table_store import save_table_rows  # noqa: E402


def _loads(raw, default):
    try:
        value = json.loads(raw or "")
        return value if isinstance(value, (list, dict)) else default
    except Exception:
        return default


def backfill(document_id: str | None = None, dry_run: bool = False) -> dict:
    maker = get_session_local()
    session = maker()
    stats = {"tables": 0, "rows": 0, "fixed_ids": 0, "fixed_quality": 0, "skipped": 0}
    try:
        query = session.query(DocumentTable)
        if document_id:
            query = query.filter(DocumentTable.document_id == document_id)
        tables = query.order_by(DocumentTable.document_id, DocumentTable.page_num,
                                 DocumentTable.table_index).all()

        for table in tables:
            rows = _loads(table.rows_json, [])
            headers = _loads(table.headers_json, [])
            if not rows:
                stats["skipped"] += 1
                continue

            table_id = table.table_id or make_table_id(
                table.document_id, table.page_num or 0, table.table_index or 0)
            if not table.table_id:
                stats["fixed_ids"] += 1

            # Качество считаем по полной таблице (шапка + строки) — так же, как в конвейере
            quality = float(table.quality or 0.0)
            if not quality:
                quality = table_quality([headers] + rows if headers else rows)
                stats["fixed_quality"] += 1

            stats["tables"] += 1
            stats["rows"] += len(rows)

            if dry_run:
                continue

            table.table_id = table_id
            table.row_count = len(rows)
            table.quality = quality
            save_table_rows(
                document_id=table.document_id,
                table_id=table_id,
                headers=headers,
                rows=rows,
                page_num=table.page_num or 0,
                session=session,
            )

        if not dry_run:
            session.commit()
        return stats
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Заполнить строчный слой таблиц")
    parser.add_argument("--document-id", default=None, help="только один документ")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать, ничего не писать")
    args = parser.parse_args()

    stats = backfill(args.document_id, args.dry_run)
    mode = "ПРОБНЫЙ ПРОГОН (ничего не записано)" if args.dry_run else "записано"
    logger.info(
        f"[backfill] {mode}: таблиц {stats['tables']}, строк {stats['rows']}, "
        f"дозаполнено table_id {stats['fixed_ids']}, качества {stats['fixed_quality']}, "
        f"пропущено пустых {stats['skipped']}"
    )
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
