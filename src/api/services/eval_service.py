"""Эталон разметки связей: страница, где человек подтверждает или исправляет
связи «требование → пункт» и «требование → мера» для набора требований.

Назначение — измеритель качества: доля пар, где связи системы совпали с ответом
человека, это точность извлечения. Без эталона «стало лучше» недоказуемо.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SEED_DOC_ID = "fb4d9a84-e723-41fa-970d-2ed75683674d"  # fstek-2014
SEED_LIMIT = 50

_DDL = """
CREATE TABLE IF NOT EXISTS eval_pairs (
    id SERIAL PRIMARY KEY,
    requirement_key TEXT NOT NULL UNIQUE,
    requirement_id TEXT,
    description TEXT,
    system_clause TEXT,
    system_measure TEXT,
    expected_clause TEXT,
    expected_measure TEXT,
    note TEXT DEFAULT '',
    annotated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""


def _eng():
    from src.database.session import get_engine

    engine = get_engine()
    with engine.begin() as c:
        from sqlalchemy import text

        c.execute(text(_DDL))
    return engine


def _read_td(q: str) -> list:
    from src.indexing import typedb_facts
    from typedb.driver import TransactionType

    cfg = typedb_facts._cfg()
    with typedb_facts._driver() as driver:
        with driver.transaction(cfg["db"], TransactionType.READ) as tx:
            return list(tx.query(q).resolve())


def _val(x: Any) -> str:
    return str(x.get("@value") if isinstance(x, dict) else (x or ""))


def _seed_rows() -> List[Dict[str, Any]]:
    """Первые требования эталонного документа с текущими связями системы."""
    prefix = SEED_DOC_ID[:16]
    rows = _read_td(
        f'match $r isa security_requirement, has source-chunk-id $c; $c like "^{prefix}"; '
        '$r has requirement-id $i, has description $d; '
        'fetch { "k": $r.ent-key, "i": $i, "d": $d };')
    out: List[Dict[str, Any]] = []
    for r in rows:
        key = _val(r.get("k"))
        rid = _val(r.get("i"))
        desc = _val(r.get("d"))
        clause = ""
        measure = ""
        try:
            crows = _read_td(
                f'match $link isa requirement_derived_from (requirement: $r, clause: $c); '
                f'$r has ent-key "{key}"; fetch {{ "v": $c.struct_index }};')
            clause = _val(crows[0].get("v")) if crows else ""
        except Exception:  # noqa: BLE001
            pass
        try:
            mrows = _read_td(
                f'match $link isa requirement_definition (requirement: $r, measure: $m); '
                f'$r has ent-key "{key}"; fetch {{ "v": $m.code }};')
            measure = _val(mrows[0].get("v")) if mrows else ""
        except Exception:  # noqa: BLE001
            pass
        out.append({"requirement_key": key, "requirement_id": rid,
                    "description": desc[:1200], "system_clause": clause,
                    "system_measure": measure})
        if len(out) >= SEED_LIMIT:
            break
    return out


def seed_if_empty() -> int:
    """Заполнить таблицу, если она пуста. Возвращает число добавленных строк."""
    from sqlalchemy import text

    engine = _eng()
    with engine.begin() as c:
        n = c.execute(text("SELECT count(*) FROM eval_pairs")).scalar()
        if n:
            return 0
        rows = _seed_rows()
        for r in rows:
            c.execute(text(
                "INSERT INTO eval_pairs (requirement_key, requirement_id, description, "
                "system_clause, system_measure) VALUES (:k, :i, :d, :c, :m) "
                "ON CONFLICT (requirement_key) DO NOTHING"),
                {"k": r["requirement_key"], "i": r["requirement_id"], "d": r["description"],
                 "c": r["system_clause"], "m": r["system_measure"]})
        return len(rows)


def list_pairs(limit: int = 20, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    from sqlalchemy import text

    seed_if_empty()
    engine = _eng()
    with engine.connect() as c:
        total = c.execute(text("SELECT count(*) FROM eval_pairs")).scalar()
        rows = c.execute(text(
            "SELECT id, requirement_key, requirement_id, description, system_clause, "
            "system_measure, expected_clause, expected_measure, note, annotated_at "
            "FROM eval_pairs ORDER BY id LIMIT :l OFFSET :o"),
            {"l": limit, "o": offset}).fetchall()
    items = []
    for r in rows:
        items.append({
            "id": r[0], "requirement_id": r[2] or "", "description": r[3] or "",
            "system_clause": r[4] or "", "system_measure": r[5] or "",
            "expected_clause": r[6] or "", "expected_measure": r[7] or "",
            "note": r[8] or "",
            "annotated": bool(r[9]),
            "clause_ok": bool(r[6]) and r[6] == r[4],
            "measure_ok": bool(r[7]) and r[7] == r[5],
        })
    return items, int(total)


def update_pair(pair_id: int, clause: str, measure: str, note: str = "") -> Dict[str, Any]:
    from sqlalchemy import text

    engine = _eng()
    with engine.begin() as c:
        res = c.execute(text(
            "UPDATE eval_pairs SET expected_clause=:c, expected_measure=:m, note=:n, "
            "annotated_at=now() WHERE id=:i"),
            {"c": (clause or "").strip()[:120], "m": (measure or "").strip()[:120],
             "n": (note or "").strip()[:300], "i": pair_id})
    if res.rowcount == 0:
        return {"status": "error", "detail": "пара не найдена"}
    return {"status": "ok"}


def stats() -> Dict[str, Any]:
    from sqlalchemy import text

    engine = _eng()
    with engine.connect() as c:
        total = c.execute(text("SELECT count(*) FROM eval_pairs")).scalar() or 0
        annotated = c.execute(text(
            "SELECT count(*) FROM eval_pairs WHERE annotated_at IS NOT NULL")).scalar() or 0
        clause_ok = c.execute(text(
            "SELECT count(*) FROM eval_pairs WHERE annotated_at IS NOT NULL "
            "AND expected_clause = system_clause AND expected_clause <> ''")).scalar() or 0
        measure_ok = c.execute(text(
            "SELECT count(*) FROM eval_pairs WHERE annotated_at IS NOT NULL "
            "AND expected_measure = system_measure AND expected_measure <> ''")).scalar() or 0
    return {
        "total": total,
        "annotated": annotated,
        "clause_accuracy": round(clause_ok / annotated, 3) if annotated else None,
        "measure_accuracy": round(measure_ok / annotated, 3) if annotated else None,
    }
