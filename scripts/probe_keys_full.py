"""Диагностика БЕЗ обрезки строк: смотрим полные ключи.

Прошлый замер обрезал ключи до 60–80 символов, и разные значения выглядели одинаковыми
(у мер ключ = длинный document_key + код, код отрезался). Здесь печатаем полные ключи,
длины и настоящие повторы.
"""
import json
from collections import Counter

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))


def fetch(tx, q, field):
    return [str(r.get(field)) for r in list(tx.query(q).resolve())]


out = {}
with d.transaction(c["db"], TransactionType.READ) as tx:
    m = fetch(tx, 'match $m isa security_measure; fetch { "v": $m.measure_key };', "v")
    out["measures"] = {
        "total": len(m),
        "distinct": len(set(m)),
        "sample": m[:2],
        "dups": Counter(m).most_common(3),
    }

    r = fetch(tx, 'match $r isa security_requirement; fetch { "v": $r.requirement_key };', "v")
    out["requirements"] = {
        "total": len(r),
        "distinct": len(set(r)),
        "sample": r[:2],
        "dups": Counter(r).most_common(3),
    }

    docs = fetch(tx, 'match $d isa regulatory_document; fetch { "v": $d.document_key };', "v")
    out["documents"] = {
        "total": len(docs),
        "distinct": len(set(docs)),
        "values": docs,
    }

print(json.dumps(out, ensure_ascii=False, indent=1))
