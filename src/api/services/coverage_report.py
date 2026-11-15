"""Паспорт покрытия документа: что извлечено и как связано.

Для выбранного документа считает по провенансу (source-chunk-id начинается с id документа):
узлы по типам и доли связей — требования с пунктом, требования с мерой, меры с пунктом.
Это «здоровье онтологии по документу» одним числом на показатель, без скриптов.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _read(q: str) -> list:
    from src.indexing import typedb_facts
    from typedb.driver import TransactionType

    cfg = typedb_facts._cfg()
    with typedb_facts._driver() as driver:
        with driver.transaction(cfg["db"], TransactionType.READ) as tx:
            return list(tx.query(q).resolve())


def _count(q: str) -> int:
    try:
        return len(_read(q))
    except Exception as exc:  # noqa: BLE001
        logger.debug("coverage: запрос не выполнен: %s", str(exc)[:120])
        return -1


def coverage_of(document_id: str) -> Dict[str, Any]:
    """Сводка по документу. document_id — uuid из таблицы documents."""
    doc_id = (document_id or "").strip()
    if not doc_id:
        return {"ok": False, "error": "не указан document_id"}

    def has_chunk(var: str = "x") -> str:
        return (f'$c like "^{doc_id[:16]}";' if len(doc_id) >= 16 else "")

    def scoped(etype: str) -> str:
        like = f'$c like "^{doc_id[:16]}";'
        return f'match $x isa {etype}, has source-chunk-id $c; {like} fetch {{ "k": $x.ent-key }};'

    def scoped_link(rel: str, role1: str, role2: str, v1: str = "$a", v2: str = "$b") -> str:
        like = f'$c like "^{doc_id[:16]}";'
        return (f'match $link isa {rel} ({role1}: {v1}, {role2}: {v2}); '
                f'{v1} has source-chunk-id $c; {like} fetch {{ "k": {v1}.ent-key }};')

    out: Dict[str, Any] = {"ok": True, "document_id": doc_id,
                           "chunks": -1, "elements": -1, "requirements": -1,
                           "measures": -1, "generic": {}}
    out["elements"] = _count(scoped("structural_element"))
    out["requirements"] = _count(scoped("security_requirement"))
    out["measures"] = _count(scoped("security_measure"))
    out["req_with_clause"] = _count(scoped_link("requirement_derived_from", "requirement", "clause"))
    out["req_with_measure"] = _count(scoped_link("requirement_definition", "requirement", "measure"))
    out["measure_with_clause"] = _count(scoped_link("measure_source", "measure", "clause"))
    for t in ("asset", "party", "threat", "vulnerability", "risk", "incident", "violation",
              "term", "business_function", "business_process", "actor_role", "data_object"):
        n = _count(scoped(t))
        if n > 0:
            out["generic"][t] = n
    out["queue_pending"] = _queue_pending(document_id)
    return out


def _queue_pending(document_id: str) -> int:
    from sqlalchemy import text
    from src.database.session import get_engine

    try:
        with get_engine().connect() as c:
            return c.execute(text(
                "SELECT count(*) FROM bronze_queue WHERE document_id = :d AND status = 'pending'"),
                {"d": document_id}).scalar() or 0
    except Exception:  # noqa: BLE001
        return -1
