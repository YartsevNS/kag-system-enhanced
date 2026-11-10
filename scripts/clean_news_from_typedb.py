"""Убрать из TypeDB документы, пришедшие из ленты новостей (коллекция news).

Зачем: до фикса конвейер писал новости в онтологию как publication. Новости в онтологии
не участвуют — их место в kag_news. Скрипт идемпотентен: повторный запуск ничего не ломает.

Запуск: python clean_news_from_typedb.py [--apply] [--limit N]
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")

from sqlalchemy import text
from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.database.session import get_session_local
from src.indexing import typedb_facts


def news_ids(limit: int = 0) -> list[str]:
    db = get_session_local()()
    try:
        sql = "select id from documents where collection = 'news'"
        if limit:
            sql += f" limit {int(limit)}"
        return [r[0] for r in db.execute(text(sql)).all()]
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    ids = news_ids(a.limit)
    print("новостей в Postgres:", len(ids))
    if not ids:
        return

    c = typedb_facts._cfg()
    drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                        DriverOptions(DriverTlsConfig.disabled()))
    removed = 0
    try:
        for doc_id in ids:
            with drv.transaction(c["db"], TransactionType.READ) as tx:
                found = list(tx.query(
                    "match $v isa document_version, has kag_document_id "
                    f'"{doc_id}"; document_has_version(version: $v, document: $d); '
                    "fetch {\"key\": $d.document_key};"
                ).resolve())
            if not found:
                continue
            removed += 1
            if not a.apply:
                continue
            with drv.transaction(c["db"], TransactionType.WRITE) as tx:
                # требования и пункты этой версии
                tx.query(
                    "match $v isa document_version, has kag_document_id "
                    f'"{doc_id}"; document_structure(version: $v, element: $e); '
                    "requirement_derived_from(clause: $e, requirement: $rq); "
                    "delete $rq;"
                ).resolve()
                tx.query(
                    "match $v isa document_version, has kag_document_id "
                    f'"{doc_id}"; document_structure(version: $v, element: $e); '
                    "delete $e;"
                ).resolve()
                tx.query(
                    "match $v isa document_version, has kag_document_id "
                    f'"{doc_id}"; document_has_version(version: $v, document: $d); '
                    "delete $v;"
                ).resolve()
                tx.query(
                    "match $v isa document_version, has kag_document_id "
                    f'"{doc_id}"; document_has_version(version: $v, document: $d); '
                    "delete $d;"
                ).resolve()
                tx.commit()
            print("  удалено:", doc_id, flush=True)
    finally:
        drv.close()
    print(("удалено" if a.apply else "будет удалено") + f": {removed}")


if __name__ == "__main__":
    main()
