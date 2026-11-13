"""Почему меры дублируются: смотрим ключи и провенанс у двух экземпляров одного кода."""
import json
from collections import Counter

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))

with d.transaction(c["db"], TransactionType.READ) as tx:
    rows = list(tx.query(
        'match $m isa security_measure; '
        'fetch { "k": $m.measure_key, "c": $m.code };').resolve())

items = [{"key": str(r.get("k"))[:80], "code": str(r.get("c"))[:30]} for r in rows]
print("всего мер:", len(items), "уникальных ключей:", len({i["key"] for i in items}))
print("топ повторов ключей:", Counter(i["key"] for i in items).most_common(3))

# берём один повторяющийся код и смотрим его экземпляры
dup_code = None
for code, n in Counter(i["code"] for i in items).most_common(6):
    if n > 1:
        dup_code = code
        break
print("код с повтором:", dup_code)
with d.transaction(c["db"], TransactionType.READ) as tx:
    rows = list(tx.query(
        f'match $m isa security_measure, has code "{dup_code}"; '
        'fetch { "k": $m.measure_key, "n": $m.name, "p": $m.source-chunk-id };').resolve())
for r in rows:
    print({k: str(r.get(k))[:60] for k in ("k", "n", "p")})
