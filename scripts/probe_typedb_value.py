"""Как вытащить целое из ответа TypeDB 3.x (reduce → Value(integer: N)).

Проверяем, что реально работает: isinstance(int), int(v), методы as_integer/get_integer.
Нужно для экрана «Здоровье»: там числа по типам, и «—» вместо 276 недопустимо.
"""
from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))
with d.transaction(c["db"], TransactionType.READ) as tx:
    rows = list(tx.query("match $x isa structural_element; reduce $n = count;").resolve())
v = rows[0]["n"] if isinstance(rows[0], dict) else rows[0]
print("type:", type(v))
print("repr:", repr(v)[:120])
print("dir:", [a for a in dir(v) if "int" in a.lower() or "value" in a.lower()][:12])
for expr, fn in [("int(v)", lambda: int(v)),
                 ("v.as_integer()", lambda: v.as_integer()),
                 ("v.get_integer()", lambda: v.get_integer()),
                 ("v.integer", lambda: v.integer),
                 ("str(v) → int", lambda: int(str(v).strip("Value(integer: )")))]:
    try:
        print(f"{expr:24} ->", fn())
    except Exception as e:  # noqa: BLE001
        print(f"{expr:24} FAIL {type(e).__name__}: {str(e)[:80]}")
