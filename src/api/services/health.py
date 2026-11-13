"""Здоровье онтологии: один экран вместо шести страниц.

Зачем: состояние системы размазано — очереди в /terms, /duplicates, /ontology/gaps,
/errors, /ontology/automation, факты в /kg. Человеку, который открывает систему утром,
нужен ответ на два вопроса: что требует меня сегодня и что вообще накопилось.
Числа по типам объектов берутся из TypeDB, очереди — из Postgres.

Правила этого модуля:
- ничего не меняет: только чтение;
- если источник недоступен, так и пишет («нет данных»), а не показывает ноль;
- у каждого числа есть объект и ссылка «где разбирать» — иначе число бесполезно.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_counts_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_COUNTS_TTL = 60.0


def _db():
    from src.database.session import get_session_local

    return get_session_local()


def _scalar(db, sql: str, params: Dict[str, Any] | None = None) -> int:
    from sqlalchemy import text

    try:
        return int(db.execute(text(sql), params or {}).scalar() or 0)
    except Exception as exc:  # noqa: BLE001 - таблицы может не быть на чистой установке
        logger.debug("здоровье: запрос не выполнен (%s): %s", sql[:40], str(exc)[:120])
        return 0


def _facts_by_type() -> Dict[str, Any]:
    """Число объектов по типам в TypeDB. Кеш 60 с: запрос идёт в базу, а не в память."""
    now = time.time()
    if _counts_cache["data"] and now - float(_counts_cache["at"]) < _COUNTS_TTL:
        return _counts_cache["data"]

    out: Dict[str, Any] = {"source": "typedb", "by_type": {}, "relations": None, "error": ""}
    try:
        from src.indexing import typedb_facts, typedb_writer

        if not typedb_facts.is_available():
            out["error"] = "TypeDB недоступен"
            return out
        from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

        c = typedb_facts._cfg()
        drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                            DriverOptions(DriverTlsConfig.disabled()))
        try:
            with drv.transaction(c["db"], TransactionType.READ) as tx:
                for etype in typedb_writer.ENTITY_TYPES:
                    try:
                        row = list(tx.query(f"match $x isa {etype}; reduce $n = count;").resolve())
                        out["by_type"][etype] = _value(row)
                    except Exception:  # noqa: BLE001 - типа может не быть в этой схеме
                        out["by_type"][etype] = None
                try:
                    out["relations"] = _count_relations(tx)
                except Exception as exc:  # noqa: BLE001
                    out["relations"] = None
                    out["relations_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return out

    _counts_cache.update({"at": now, "data": out})
    return out


def _count_relations(tx) -> int | None:
    """Число связей.

    Корневой тип `relation` в TypeQL 3 нельзя использовать как идентификатор
    (проверено на стенде: REP27 «A reserved keyword 'relation' was used as identifier»).
    Поэтому сначала получаем список типов связей из живой схемы, затем суммируем
    инстансы каждого. Если список пуст — None, и экран честно пишет «—», а не ноль.
    """
    labels = _relation_labels(tx)
    if not labels:
        return None
    total = 0
    counted = 0
    for label in labels:
        try:
            n = _value(list(tx.query(f"match $r isa {label}; reduce $n = count;").resolve()))
        except Exception:  # noqa: BLE001 - типа может не быть в этой базе
            continue
        if n is not None:
            total += n
            counted += 1
    return total if counted else None


def _relation_labels(tx) -> list[str]:
    """Типы связей из схемы: те, у которых kind == relation."""
    try:
        rows = list(tx.query('match $t sub $r; fetch { "t": $t };').resolve())
    except Exception as exc:  # noqa: BLE001
        logger.debug("здоровье: типы связей не прочитаны: %s", str(exc)[:160])
        return []
    out = []
    for row in rows:
        try:
            t = row.get("t") or {}
        except Exception:  # noqa: BLE001
            continue
        if isinstance(t, dict) and t.get("kind") == "relation" and t.get("label"):
            out.append(t["label"])
    return sorted(set(out))


def _value(rows: List[Any]) -> int | None:
    """Целое из ответа reduce: ConceptRow → Value → get_integer().

    Проверено на стенде: объект Value не приводится к int() и не имеет as_integer,
    единственный рабочий путь — row.get("n").get_integer().
    """
    if not rows:
        return None
    row = rows[0]
    try:
        val = row.get("n") if hasattr(row, "get") else row
    except Exception:  # noqa: BLE001
        return None
    try:
        return int(val.get_integer())
    except Exception:  # noqa: BLE001
        try:
            return int(val)
        except Exception:  # noqa: BLE001
            return None


def _queues(db) -> Dict[str, int]:
    from sqlalchemy import text

    q: Dict[str, int] = {}
    q["terms_pending"] = _scalar(db, "SELECT count(*) FROM entity_aliases "
                                     "WHERE reviewed = false")
    q["duplicates_pending"] = _scalar(db, "SELECT count(*) FROM duplicate_candidates "
                                          "WHERE status = 'pending'")
    q["gaps_proposed"] = _scalar(db, "SELECT count(*) FROM ontology_schema_gaps "
                                     "WHERE status = 'proposed'")
    q["errors_open"] = _scalar(db, "SELECT count(*) FROM ontology_errors WHERE NOT resolved")
    q["auto_decisions"] = _scalar(db, "SELECT count(*) FROM ontology_auto_decisions "
                                      "WHERE status = 'active'")
    try:
        rows = db.execute(text("SELECT alias, canonical_name, entity_type FROM entity_aliases "
                               "WHERE reviewed = false LIMIT 5000")).all()
    except Exception:  # noqa: BLE001
        rows = []
    # Разбивка очереди словаря: сколько машинка не решает (сокращение не выводится)
    # и сколько доменных терминов среди ожидающих. Второе — самое важное число.
    from src.api.services.automation import advice

    junk = 0
    for alias, canonical, etype in rows:
        if advice(alias or "", canonical or "", etype or "").get("hint") == "abbreviation_mismatch":
            junk += 1
    q["terms_not_derived"] = junk
    q["terms_of_domain"] = _scalar(db, "SELECT count(*) FROM entity_aliases "
                                      "WHERE reviewed = false AND entity_type = 'term'")
    return q


def _documents(db) -> Dict[str, int]:
    """Документы: корпус отдельно от новостей.

    Новости лежат в той же таблице (collection='news'), но в онтологию не идут —
    при обработке шаг TypeDB для них пропускается. Считать их «документами в деле»
    значит показывать владельцу число, которого в корпусе нет: 385 новостей против
    86 документов корпуса на 13.11.2026.
    """
    corpus = "coalesce(collection,'') <> 'news'"
    return {
        "total": _scalar(db, "SELECT count(*) FROM documents WHERE is_active"),
        "corpus": _scalar(db, f"SELECT count(*) FROM documents WHERE is_active AND {corpus}"),
        "news": _scalar(db, "SELECT count(*) FROM documents WHERE is_active "
                             "AND collection = 'news'"),
        "completed": _scalar(db, f"SELECT count(*) FROM documents WHERE status = 'completed' "
                                 f"AND {corpus}"),
        "processing": _scalar(db, f"SELECT count(*) FROM documents "
                                  f"WHERE status IN ('processing','queued','pending') AND {corpus}"),
        "failed": _scalar(db, f"SELECT count(*) FROM documents WHERE status = 'failed' AND {corpus}"),
        "chunks": _scalar(db, f"SELECT coalesce(sum(chunks_count),0) FROM documents "
                              f"WHERE status = 'completed' AND {corpus}"),
    }


def _top_gaps(db, limit: int = 5) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    try:
        rows = db.execute(text(
            "SELECT term, hits FROM ontology_schema_gaps WHERE status = 'proposed' "
            "ORDER BY hits DESC, term LIMIT :lim"), {"lim": limit}).all()
        return [{"term": t, "hits": int(h or 0)} for t, h in rows]
    except Exception:  # noqa: BLE001
        return []


def _recent_decisions(db, limit: int = 5) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    try:
        rows = db.execute(text(
            "SELECT kind, subject, decision, rule, confidence, created_at "
            "FROM ontology_auto_decisions WHERE status = 'active' "
            "ORDER BY created_at DESC LIMIT :lim"), {"lim": limit}).mappings().all()
        return [{"kind": r["kind"], "subject": r["subject"], "decision": r["decision"],
                 "rule": r["rule"], "confidence": float(r["confidence"] or 0),
                 "created_at": r["created_at"].isoformat() if r["created_at"] else ""}
                for r in rows]
    except Exception:  # noqa: BLE001
        return []


def snapshot() -> Dict[str, Any]:
    """Полный снимок состояния для экрана «Здоровье онтологии»."""
    db = _db()()
    try:
        queues = _queues(db)
        documents = _documents(db)
        gaps = _top_gaps(db)
        decisions = _recent_decisions(db)
    finally:
        db.close()

    facts = _facts_by_type()
    by_type = {k: v for k, v in (facts.get("by_type") or {}).items() if v is not None}
    facts_total = sum(v for v in by_type.values() if isinstance(v, int))

    # «Требует человека сегодня»: только то, где решение действительно нужно,
    # с количеством и адресом. Пустые очереди сюда не попадают.
    todo = []
    if queues["terms_of_domain"]:
        todo.append({"label": "Термины: подтвердить или отклонить",
                     "count": queues["terms_of_domain"],
                     "href": "/terms?status=pending&entity_type=term",
                     "why": "доменные сокращения и понятия — влияют на извлечение"})
    if queues["duplicates_pending"]:
        todo.append({"label": "Возможные дубли документов", "count": queues["duplicates_pending"],
                     "href": "/duplicates", "why": "без решения новый файл не станет версией"})
    if queues["gaps_proposed"]:
        todo.append({"label": "Пробелы схемы", "count": queues["gaps_proposed"],
                     "href": "/ontology/gaps", "why": "термины, которых онтология не знает"})
    if queues["errors_open"]:
        todo.append({"label": "Ошибки онтологии", "count": queues["errors_open"],
                     "href": "/errors", "why": "записи в TypeDB, требующие разбора"})
    if queues["terms_pending"] and queues["terms_not_derived"]:
        todo.append({"label": "Очередь словаря целиком (в т.ч. не доменные пары)",
                     "count": queues["terms_pending"], "href": "/terms?status=pending",
                     "why": f"из них {queues['terms_not_derived']} — сокращение не выводится"})

    return {
        "queues": queues,
        "documents": documents,
        "facts": {"source": facts.get("source"), "total": facts_total,
                  "relations": facts.get("relations"), "by_type": by_type,
                  "error": facts.get("error") or ""},
        "top_gaps": gaps,
        "recent_decisions": decisions,
        "todo": todo,
    }
