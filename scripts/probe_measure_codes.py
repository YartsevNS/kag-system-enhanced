"""Сколько мер имеют код, а сколько — только название группы.

Мера без кода (АВЗ.1, ИАФ.2) не является мерой: это заголовок направления.
Если таких большинство — их не нужно писать в онтологию как объекты.
"""
import json

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))

with d.transaction(c["db"], TransactionType.READ) as tx:
    total = list(tx.query("match $m isa security_measure; reduce $n = count;").resolve())
    with_code = list(tx.query(
        'match $m isa security_measure, has code $c; reduce $n = count;').resolve())
    keys = [str(r.get("v")) for r in list(tx.query(
        'match $m isa security_measure; fetch { "v": $m.measure_key };').resolve())]
    codes = [str(r.get("v")) for r in list(tx.query(
        'match $m isa security_measure, has code $c; fetch { "v": $c };').resolve())]

print(json.dumps({
    "measures_total": total[0].get("n").get_integer() if total else 0,
    "measures_with_code": with_code[0].get("n").get_integer() if with_code else 0,
    "distinct_keys": len(set(keys)),
    "distinct_codes": len(set(codes)),
    "codes_sample": sorted(set(codes))[:10],
}, ensure_ascii=False, indent=1))
