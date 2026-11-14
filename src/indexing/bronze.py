"""Bronze-слой: сырое извлечение до онтологии.

Зачем (техдолг п.20 и разбор внешнего плана): сейчас извлечение сразу пишет в TypeDB, поэтому
ошибку модели нельзя пересмотреть без повторного вызова модели, решение «один объект или два»
принимается в момент записи, а что именно извлекла модель — нигде не остаётся.

Здесь: каждый прогон документа пишет строки (сущности и связи) в Postgres как есть, с ключом
прогона `extraction_run`. Дальше по этим строкам можно детерминированно считать диф прогонов,
применять нормализацию и дедупликацию, и только потом писать в TypeDB.

Ничего в текущем пути не меняет: запись в TypeDB остаётся, Bronze — параллельный след.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS bronze_runs (
        run_id        text PRIMARY KEY,
        document_id   text NOT NULL,
        filename      text NOT NULL DEFAULT '',
        model         text NOT NULL DEFAULT '',
        prompt_version integer NOT NULL DEFAULT 0,
        started_at    timestamptz NOT NULL DEFAULT now(),
        finished_at   timestamptz,
        chunks        integer NOT NULL DEFAULT 0,
        entities      integer NOT NULL DEFAULT 0,
        relations     integer NOT NULL DEFAULT 0,
        empty_chunks  integer NOT NULL DEFAULT 0,
        note          text NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS extraction_bronze (
        id             bigserial PRIMARY KEY,
        run_id         text NOT NULL,
        document_id    text NOT NULL,
        chunk_id       text NOT NULL,
        clause         text NOT NULL DEFAULT '',
        kind           text NOT NULL,
        type           text NOT NULL,
        payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
        participants   jsonb NOT NULL DEFAULT '[]'::jsonb,
        raw_ent_key    text NOT NULL DEFAULT '',
        created_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS extraction_bronze_run_idx ON extraction_bronze (run_id)",
    "CREATE INDEX IF NOT EXISTS extraction_bronze_doc_idx ON extraction_bronze (document_id)",
    "CREATE INDEX IF NOT EXISTS extraction_bronze_key_idx ON extraction_bronze (raw_ent_key)",
]

_ready = False


def _db():
    from src.database.session import get_session_local

    return get_session_local()


def ensure_tables() -> None:
    global _ready
    if _ready:
        return
    from sqlalchemy import text

    db = _db()()
    try:
        for stmt in _DDL:
            db.execute(text(stmt))
        db.commit()
        _ready = True
    except Exception as exc:  # noqa: BLE001 - Bronze не должен ломать обработку
        logger.warning("bronze: таблицы не созданы: %s", str(exc)[:200])
    finally:
        db.close()


def start_run(document_id: str, filename: str = "", model: str = "",
              prompt_version: int = 0) -> str:
    """Открыть прогон извлечения. Возвращает run_id (или "" если Bronze недоступен)."""
    ensure_tables()
    from sqlalchemy import text

    run_id = uuid.uuid4().hex
    db = _db()()
    try:
        db.execute(text(
            "INSERT INTO bronze_runs (run_id, document_id, filename, model, prompt_version) "
            "VALUES (:r, :d, :f, :m, :v)"),
            {"r": run_id, "d": document_id, "f": filename or "", "m": model or "",
             "v": int(prompt_version or 0)})
        db.commit()
        return run_id
    except Exception as exc:  # noqa: BLE001
        logger.debug("bronze: прогон не открыт: %s", str(exc)[:160])
        return ""
    finally:
        db.close()


def write_chunk(run_id: str, document_id: str, chunk_id: str, clause: str,
                entities: Dict[str, Any], relations: Dict[str, Any]) -> int:
    """Записать сырые сущности и связи фрагмента. Возвращает число строк."""
    if not run_id or (not entities and not relations):
        return 0
    from sqlalchemy import text

    rows: List[Dict[str, Any]] = []
    for key, e in (entities or {}).items():
        rows.append({"kind": "entity", "type": str(e.get("type") or ""),
                     "payload": json.dumps(e.get("attrs") or {}, ensure_ascii=False),
                     "participants": "[]", "raw_key": str(key)})
    for _sig, rel in (relations or {}).items():
        rows.append({"kind": "relation", "type": str(rel.get("type") or ""),
                     "payload": "{}",
                     "participants": json.dumps(rel.get("parts") or {}, ensure_ascii=False),
                     "raw_key": ""})
    if not rows:
        return 0

    db = _db()()
    try:
        for r in rows:
            db.execute(text(
                "INSERT INTO extraction_bronze "
                "(run_id, document_id, chunk_id, clause, kind, type, payload, participants, raw_ent_key) "
                "VALUES (:run, :doc, :chunk, :clause, :kind, :type, cast(:payload as jsonb), "
                "cast(:parts as jsonb), :raw)"),
                {"run": run_id, "doc": document_id, "chunk": chunk_id, "clause": clause or "",
                 "kind": r["kind"], "type": r["type"], "payload": r["payload"],
                 "parts": r["participants"], "raw": r["raw_key"]})
        db.commit()
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.debug("bronze: строки не записаны: %s", str(exc)[:160])
        return 0
    finally:
        db.close()


def finish_run(run_id: str, chunks: int, entities: int, relations: int,
               empty_chunks: int, note: str = "") -> None:
    if not run_id:
        return
    from sqlalchemy import text

    db = _db()()
    try:
        db.execute(text(
            "UPDATE bronze_runs SET finished_at = now(), chunks = :c, entities = :e, "
            "relations = :r, empty_chunks = :z, note = :n WHERE run_id = :id"),
            {"c": int(chunks), "e": int(entities), "r": int(relations),
             "z": int(empty_chunks), "n": (note or "")[:1000], "id": run_id})
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("bronze: прогон не закрыт: %s", str(exc)[:160])
    finally:
        db.close()


def runs(document_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    ensure_tables()
    db = _db()()
    try:
        rows = db.execute(text(
            "SELECT run_id, filename, started_at, finished_at, chunks, entities, relations, "
            "empty_chunks, model, prompt_version, note FROM bronze_runs WHERE document_id = :d "
            "ORDER BY started_at DESC LIMIT :l"), {"d": document_id, "l": limit}).mappings().all()
    finally:
        db.close()
    out = []
    for r in rows:
        it = dict(r)
        for k in ("started_at", "finished_at"):
            it[k] = it[k].isoformat() if it.get(k) else None
        out.append(it)
    return out


def _keys(run_id: str) -> Dict[str, set]:
    from sqlalchemy import text

    db = _db()()
    try:
        rows = db.execute(text(
            "SELECT kind, raw_ent_key FROM extraction_bronze WHERE run_id = :r"), {"r": run_id}
        ).all()
    finally:
        db.close()
    out: Dict[str, set] = {"entity": set(), "relation": set()}
    for kind, key in rows:
        out.setdefault(kind, set()).add(key or "")
    return out


def compare(document_id: str, run_a: str, run_b: str) -> Dict[str, Any]:
    """Диф двух прогонов по ключам: что совпало, что появилось, что исчезло.

    Это прямой ответ на техдолг п.20: видно, ЧТО модель теряет от прогона к прогону,
    а не только сколько всего получилось.
    """
    a, b = _keys(run_a), _keys(run_b)
    res: Dict[str, Any] = {"document_id": document_id, "run_a": run_a, "run_b": run_b}
    for kind in ("entity", "relation"):
        ka = {k for k in a.get(kind, set()) if k}
        kb = {k for k in b.get(kind, set()) if k}
        res[kind] = {
            "в_a": len(ka),
            "в_b": len(kb),
            "совпало": len(ka & kb),
            "появилось": sorted(kb - ka)[:20],
            "исчезло": sorted(ka - kb)[:20],
            "появилось_всего": len(kb - ka),
            "исчезло_всего": len(ka - kb),
        }
    return res
