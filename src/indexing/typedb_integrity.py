"""Проверки целостности онтологии v3: ролевые ограничения, которых нет в схеме TypeDB.

Почему отдельным шагом, а не аннотацией: в TypeDB 3.14 `@key`/`@unique` работают
только для владения атрибутами (`owns`). На ролях связи (`relates`) они не
поддерживаются — [ANN5] Annotation '@unique' is not supported for relates
(проверено применением; см. также typedb/typedb issue #7030 «Role players as keys»).

Поэтому ограничения вида «одна текущая редакция на документ», «один родитель у пункта»,
«пункт принадлежит одной редакции», «один издатель (если не совместный приказ)»
проверяются функциями. Функции возвращают САМИ нарушения с участниками, а не «есть/нет».

Вызывать после записи документа и ПЕРЕД тем, как считать загрузку успешной.
Непустой результат — либо откат (если пишем в одной транзакции), либо явная
пометка «загружено с нарушениями целостности» в результате обработки.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# имя функции -> (запрос, как назвать нарушение)
_CHECKS = (
    ("multiple_current_versions",
     'match let $d, $v in multiple_current_versions(); '
     'fetch {"документ": $d.title, "редакция": $v.version_string};',
     "у документа больше одной действующей редакции"),
    ("multiple_parents",
     'match let $c, $p1, $p2 in multiple_parents(); '
     'fetch {"пункт": $c.struct_index};',
     "у пункта несколько родителей"),
    ("element_in_multiple_structures",
     'match let $e, $v1, $v2 in element_in_multiple_structures(); '
     'fetch {"пункт": $e.struct_index};',
     "пункт принадлежит нескольким редакциям"),
    ("current_not_in_history",
     'match let $d, $v in current_not_in_history(); '
     'fetch {"документ": $d.title, "редакция": $v.version_string};',
     "текущая редакция отсутствует в истории документа"),
    ("documents_without_issuer",
     'match let $d in documents_without_issuer(); fetch {"документ": $d.title};',
     "у нормативного акта нет издателя"),
    ("elements_without_version",
     'match let $e in elements_without_version(); '
     'fetch {"пункт": $e.struct_index};',
     "пункт не привязан ни к одной редакции"),
    ("versions_without_document",
     'match let $v in versions_without_document(); '
     'fetch {"редакция": $v.version_string};',
     "редакция не привязана к документу"),
)


def _cfg() -> Dict[str, str]:
    try:
        from src.config import get_settings

        s = get_settings()
        return {"addr": s.TYPEDB_ADDR, "db": s.TYPEDB_DB,
                "user": s.TYPEDB_USER, "pass": s.TYPEDB_PASS}
    except Exception:  # noqa: BLE001
        return {"addr": os.getenv("TYPEDB_ADDR", "localhost:1729"),
                "db": os.getenv("TYPEDB_DB", "cybersecurity_ontology"),
                "user": os.getenv("TYPEDB_USER", "admin"),
                "pass": os.getenv("TYPEDB_PASS", "")}


def check_integrity(limit_per_check: int = 50) -> List[Dict[str, Any]]:
    """Вернуть список нарушений. Пустой список — целостность соблюдена.

    Функции, которых нет в базе (старая схема v2), пропускаются: проверка не должна
    падать на несовместимой схеме, она должна сказать, что проверить не удалось.
    """
    from typedb.driver import (
        Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB,
    )

    cfg = _cfg()
    problems: List[Dict[str, Any]] = []
    try:
        drv = TypeDB.driver(cfg["addr"], Credentials(cfg["user"], cfg["pass"]),
                            DriverOptions(DriverTlsConfig.disabled()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("целостность: TypeDB недоступна: %s", str(exc)[:160])
        return problems
    try:
        for name, q, title in _CHECKS:
            try:
                with drv.transaction(cfg["db"], TransactionType.READ) as tx:
                    rows = list(tx.query(q).resolve())
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).splitlines()[0][:120]
                if "QEX" in msg or "TSV" in msg:
                    logger.info("целостность: проверка %s недоступна в этой схеме", name)
                else:
                    logger.warning("целостность: %s не выполнена: %s", name, msg)
                continue
            for row in rows[:limit_per_check]:
                problems.append({
                    "check": name,
                    "problem": title,
                    "subject": {k: (v.get("@value") if isinstance(v, dict) else v)
                                for k, v in row.items()},
                })
    finally:
        try:
            drv.close()
        except Exception:
            pass
    if problems:
        logger.warning("целостность онтологии: %d нарушений (%s)",
                       len(problems), ", ".join(sorted({p["check"] for p in problems})))
    return problems
