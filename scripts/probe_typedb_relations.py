"""Подбор запроса: как посчитать СВЯЗИ в TypeDB 3.x (не сущности).

Варианты проверяются по очереди, печатается первый рабочий и его число.
Нужно для экрана «Здоровье»: «связей: —» выглядит как отсутствие данных.
"""
from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))

variants = [
    "match relation $r; reduce $n = count;",
    "match $t sub relation; $r isa $t; reduce $n = count;",
    "match $t type relation; reduce $n = count;",
    "match $t sub relation; fetch { \"name\": label($t) };",
]
with d.transaction(c["db"], TransactionType.READ) as tx:
    for q in variants:
        try:
            rows = list(tx.query(q).resolve())
            sample = rows[0].get("n") if rows and hasattr(rows[0], "get") else None
            val = None
            if sample is not None:
                try:
                    val = sample.get_integer()
                except Exception:  # noqa: BLE001
                    val = repr(sample)[:60]
            print("OK  ", q[:60], "->", val if val is not None else f"строк {len(rows)}")
        except Exception as e:  # noqa: BLE001
            print("FAIL", q[:60], "->", type(e).__name__, str(e)[:160])
