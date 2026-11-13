"""Подбор синтаксиса подсчёта в TypeDB 3.x (нужен для экрана «Здоровье онтологии»).

Проверяем три варианта агрегата на одном типе и печатаем, что отвечает драйвер:
ошибки не глотаем, иначе экран покажет нули вместо фактов.
"""
from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
db = c["db"]
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))

variants = [
    "match $x isa structural_element; reduce $n = count;",
    "match $x isa structural_element; reduce $n = count($x);",
    'match $x isa structural_element; fetch { "n": iid($x) };',
]
with d.transaction(db, TransactionType.READ) as tx:
    for q in variants:
        try:
            rows = list(tx.query(q).resolve())
            first = rows[0] if rows else None
            val = None
            if isinstance(first, dict):
                val = first
            elif first is not None:
                try:
                    val = {k: first.get(k) for k in ("n",)}
                except Exception:
                    val = str(first)[:120]
            print("OK  ", q, "->", val if val is not None else f"строк {len(rows)}")
        except Exception as e:  # noqa: BLE001
            print("FAIL", q, "->", type(e).__name__, str(e)[:220])
