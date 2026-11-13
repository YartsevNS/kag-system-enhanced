"""Второй шаг подбора: что даёт объект Value и как из него получить целое."""
from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))
with d.transaction(c["db"], TransactionType.READ) as tx:
    row = list(tx.query("match $x isa structural_element; reduce $n = count;").resolve())[0]
print("row type:", type(row).__name__)
print("row.get('n') repr:", repr(row.get("n"))[:100])
v = row.get("n")
print("value type:", type(v).__name__)
print("value dir:", [a for a in dir(v) if not a.startswith("_")][:20])
for expr, fn in [("int(v)", lambda: int(v)),
                 ("v.as_integer()", lambda: v.as_integer()),
                 ("v.get_integer()", lambda: v.get_integer()),
                 ("v.integer", lambda: v.integer),
                 ("v.as_int()", lambda: v.as_int())]:
    try:
        print(f"{expr:18} ->", fn())
    except Exception as e:  # noqa: BLE001
        print(f"{expr:18} FAIL {type(e).__name__}: {str(e)[:70]}")
