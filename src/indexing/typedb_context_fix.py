"""Исправление контекста документа: ручная правка вида/номера/издателя/даты после ошибки.

Зачем: если парсер шапки не распознал документ, он попадает в онтологию с видом other и
ключом-заглушкой. Правильное место исправления — запись в журнале ошибок, а не повторная
загрузка файла: файл уже разобран, чанки и векторы построены, терять их нельзя.

Что делает:
  1) сливает новые значения в source_metadata (Postgres) и пересчитывает document_type;
  2) пересчитывает ключ документа одной функцией (doc_context.build_context);
  3) в TypeDB меняет у документа document_key/doc_type/number и переписывает
     requirement_key у связанных требований (ключ = document_key|пункт).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _q(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace('"', '\\"')


def _document_and_keys(document_id: str) -> Tuple[str, str, List[str]]:
    """Текущий document_key, doc_type и список requirement_key документа."""
    from typedb.driver import TransactionType

    from src.indexing import typedb_facts

    cfg = typedb_facts._cfg()
    drv = typedb_facts._driver()
    key, kind, req_keys = "", "", []
    try:
        with drv.transaction(cfg["db"], TransactionType.READ) as tx:
            rows = list(tx.query(
                "match $v isa document_version, has kag_document_id "
                f'"{_q(document_id)}"; document_has_version(version: $v, document: $d); '
                'fetch {"key": $d.document_key, "kind": $d.doc_type};'
            ).resolve())
            for r in rows:
                for name in ("key", "kind"):
                    v = r.get(name)
                    val = (v.get("@value") if isinstance(v, dict) else v) or ""
                    if name == "key" and val:
                        key = val
                    if name == "kind" and val:
                        kind = val
            reqs = list(tx.query(
                "match $v isa document_version, has kag_document_id "
                f'"{_q(document_id)}"; document_structure(version: $v, element: $e); '
                "requirement_derived_from(clause: $e, requirement: $rq); "
                'fetch {"rk": $rq.requirement_key};'
            ).resolve())
            for r in reqs:
                v = r.get("rk")
                val = (v.get("@value") if isinstance(v, dict) else v) or ""
                if val:
                    req_keys.append(val)
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass
    return key, kind, req_keys


def _rewrite_document(old_key: str, new_key: str, doc_type: str, number: str) -> None:
    """Переписать у документа ключ/вид/номер и пересчитать requirement_key требований."""
    from typedb.driver import TransactionType

    from src.indexing import typedb_facts

    cfg = typedb_facts._cfg()
    drv = typedb_facts._driver()
    try:
        with drv.transaction(cfg["db"], TransactionType.WRITE) as tx:
            # 1) ключ документа: удалить старый и вставить новый одной инструкцией
            tx.query(
                'match $d isa document, has document_key $k; '
                f'$k == "{_q(old_key)}"; '
                "delete has $k of $d; "
                f'insert $d has document_key "{_q(new_key)}";'
            ).resolve()

            # 2) вид и номер
            if doc_type:
                tx.query(
                    'match $d isa document, has document_key $k, has doc_type $t; '
                    f'$k == "{_q(new_key)}"; '
                    "delete has $t of $d; "
                    f'insert $d has doc_type "{_q(doc_type)}";'
                ).resolve()
            if number:
                tx.query(
                    'match $d isa regulatory_document, has document_key $k; '
                    f'$k == "{_q(new_key)}"; '
                    f'insert $d has number "{_q(number)}";'
                ).resolve()

            # 3) requirement_key = document_key|пункт — пересобрать у всех требований документа
            reqs = list(tx.query(
                'match $d isa document, has document_key $k; '
                f'$k == "{_q(new_key)}"; '
                "document_has_version(version: $v, document: $d); "
                "document_structure(version: $v, element: $e); "
                "requirement_derived_from(clause: $e, requirement: $rq); "
                'fetch {"rk": $rq.requirement_key};'
            ).resolve())
            for r in reqs:
                v = r.get("rk")
                cur = (v.get("@value") if isinstance(v, dict) else v) or ""
                if not cur or "|" not in cur:
                    continue
                clause = cur.rsplit("|", 1)[-1]
                new_req_key = f"{new_key}|{clause}"
                if new_req_key == cur:
                    continue
                tx.query(
                    'match $rq isa security_requirement, has requirement_key $k; '
                    f'$k == "{_q(cur)}"; '
                    "delete has $k of $rq; "
                    f'insert $rq has requirement_key "{_q(new_req_key)}";'
                ).resolve()
            tx.commit()
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass


def fix_context(document_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    """Применить ручные значения к документу. Возвращает старый и новый ключи."""
    from src.database.session import get_session_local
    from src.indexing import doc_context, document_kinds
    from sqlalchemy import text

    db = get_session_local()()
    try:
        row = db.execute(
            text("select id, filename, recognized_title, source_metadata, document_type "
                 "from documents where id = :id"), {"id": document_id},
        ).mappings().first()
        if not row:
            return {"ok": False, "error": "документ не найден"}

        sm = {}
        raw = row["source_metadata"]
        if isinstance(raw, str) and raw.strip():
            try:
                sm = json.loads(raw)
            except Exception:  # noqa: BLE001
                sm = {}
        elif isinstance(raw, dict):
            sm = dict(raw)

        old_key, old_kind, req_keys = _document_and_keys(document_id)

        # новые значения: только заданные и валидные
        for field in ("issuer", "number", "version_string", "effective_from"):
            val = str(changes.get(field) or "").strip()
            if val:
                sm[field] = val
        kind = str(changes.get("doc_kind") or "").strip()
        if kind:
            if not document_kinds.is_valid(kind):
                return {"ok": False, "error": f"вид «{kind}» не из словаря"}
            sm["doc_kind"] = kind
        sm.pop("needs_review", None)
        sm.pop("parsed_missing", None)

        title = row["recognized_title"] or row["filename"] or ""
        ctx = doc_context.build_context(sm, {"recognized_title": title}, title=title,
                                        document_id=document_id)
        new_key = ctx.get("document_key") or ""
        new_kind = ctx.get("doc_type") or ""
        if not new_key or "|" not in new_key:
            return {"ok": False,
                    "error": "ключ не строится: нужны издатель, вид и номер (или название)"}

        db.execute(
            text("update documents set source_metadata = :sm, document_type = :dt, "
                 "updated_at = now() where id = :id"),
            {"sm": json.dumps(sm, ensure_ascii=False), "dt": new_kind or row["document_type"],
             "id": document_id},
        )
        db.commit()
    finally:
        db.close()

    if old_key and old_key != new_key:
        _rewrite_document(old_key, new_key, new_kind, ctx.get("number") or "")

    # Записи журнала по этому документу закрываем сами: правка контекста и есть их решение.
    # Иначе человек исправил, а /errors продолжает показывать те же ошибки открытыми.
    try:
        db2 = get_session_local()()
        try:
            db2.execute(
                text("update ontology_errors set resolved = true, resolved_at = now(), "
                     "resolved_by = 'manual_fix' where document_id = :id and not resolved"),
                {"id": document_id},
            )
            db2.commit()
        finally:
            db2.close()
    except Exception as exc:  # noqa: BLE001 — журнал не должен валить правку
        logger.warning("исправление контекста: записи журнала не закрыты: %s", str(exc)[:160])

    return {"ok": True, "document_id": document_id, "old_key": old_key, "new_key": new_key,
            "doc_type": new_kind, "number": ctx.get("number") or "",
            "requirements": len(req_keys), "source_metadata": sm}
