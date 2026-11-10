"""Загрузка снимка БДУ ФСТЭК (уязвимости) в TypeDB.

Источник — локальный снимок SQLite из зеркала velvetway/bdu-fstec-mirror
(данные ФСТЭК, код зеркала MIT). Снимок не тянем из сети в контуре заказчика:
файл привозят и указывают путь.

Что делает:
  * читает таблицу vulnerabilities (id, name, description, severity_level, cvss_score,
    identify_date, software_names, vendors);
  * пишет сущности vulnerability с ключом bdu-id и провенансом source_origin
    («bdu-snapshot-<дата снимка>»);
  * идемпотентно: повторный запуск не создаёт дубли (put по ent-key).

Использование (внутри контейнера kag-api или локально с доступом к TypeDB):
  python scripts/load_bdu.py --sqlite /tmp/bdu.sqlite --limit 300
  python scripts/load_bdu.py --sqlite /tmp/bdu.sqlite --limit 0        # весь снимок
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from typing import Any, Dict, Iterable, List


def _q(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def _date(value: str) -> str:
    s = str(value or "").strip()[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else ""


def _escape_list(value: str, limit: int = 300) -> str:
    return (value or "").strip().replace("\n", " ")[:limit]


def rows_from_sqlite(path: str, limit: int = 0) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("select id, name, description, severity_level, cvss_score, identify_date, "
               "software_names, vendors from vulnerabilities order by rowid desc")
        if limit and limit > 0:
            sql += f" limit {int(limit)}"
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def build_query(row: Dict[str, Any], snapshot: str) -> str:
    bdu = (row.get("id") or "").strip()
    if not bdu:
        return ""
    parts = [
        f'has ent-key "vulnerability:{_q(bdu)}"',
        f'has bdu-id "{_q(bdu)}"',
        f'has source_origin "{_q(snapshot)}"',
    ]
    name = _escape_list(row.get("name") or "", 300)
    if name:
        parts.append(f'has name "{_q(name)}"')
    desc = _escape_list(row.get("description") or "", 2000)
    if desc:
        parts.append(f'has description "{_q(desc)}"')
    sev = row.get("severity_level")
    if sev not in (None, ""):
        try:
            parts.append(f"has severity-level {int(sev)}")
        except (TypeError, ValueError):
            pass
    score = row.get("cvss_score")
    if score not in (None, ""):
        try:
            parts.append(f"has cvss-score {float(score)}")
        except (TypeError, ValueError):
            pass
    d = _date(row.get("identify_date") or "")
    if d:
        parts.append(f"has published-date {d}")
    for sw in _split_software(row.get("software_names") or ""):
        parts.append(f'has affected-software "{_q(sw)}"')
    # put, а не insert: повторная загрузка того же снимка обновляет сущность,
    # а не падает на @key bdu-id
    return "put\n  $v isa vulnerability, " + ",\n    ".join(parts) + ";"


def _split_software(value: str, limit: int = 5) -> List[str]:
    out = []
    for part in re.split(r"[;\n]", value):
        p = part.strip()
        if p and len(p) <= 200:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def load(sqlite_path: str, limit: int, batch: int, snapshot: str, dry_run: bool) -> int:
    rows = rows_from_sqlite(sqlite_path, limit)
    print(f"строк к загрузке: {len(rows)} (лимит {limit or 'весь снимок'})")
    queries = [q for q in (build_query(r, snapshot) for r in rows) if q]
    if dry_run:
        print("пример запроса:\n" + queries[0][:400])
        return 0

    sys.path.insert(0, "/app")
    from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

    from src.indexing import typedb_facts

    c = typedb_facts._cfg()
    drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                        DriverOptions(DriverTlsConfig.disabled()))
    written = 0
    try:
        for start in range(0, len(queries), batch):
            chunk = queries[start:start + batch]
            with drv.transaction(c["db"], TransactionType.WRITE) as tx:
                for q in chunk:
                    tx.query(q).resolve()
                tx.commit()
            written += len(chunk)
            print(f"  записано {written} из {len(queries)}", flush=True)
    finally:
        drv.close()
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Загрузка снимка БДУ в TypeDB")
    ap.add_argument("--sqlite", required=True, help="путь к bdu.sqlite (распакованный снимок)")
    ap.add_argument("--limit", type=int, default=0, help="0 = весь снимок")
    ap.add_argument("--batch", type=int, default=200, help="сколько записей в одной транзакции")
    ap.add_argument("--snapshot", default="bdu-snapshot-2026-11-07",
                    help="метка снимка для провенанса")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    n = load(a.sqlite, a.limit, max(1, a.batch), a.snapshot, a.dry_run)
    print(f"готово: записано {n}")


if __name__ == "__main__":
    main()
