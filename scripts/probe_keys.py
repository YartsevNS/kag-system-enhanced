"""Диагностика ключей: документ и меры.

Нужно понять две вещи до запуска следующих документов:
1. как заполнен document_key у документов (должно быть «издатель|тип|номер»);
2. сколлапсированы ли меры в одну запись или плодятся копии с одним ent-key.
"""
import json
from collections import Counter

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))

out = {}
with d.transaction(c["db"], TransactionType.READ) as tx:
    rows = list(tx.query('match $d isa regulatory_document; '
                         'fetch { "k": $d.document_key, "t": $d.title };').resolve())
    out["documents"] = [{"key": str(r.get("k"))[:80], "title": str(r.get("t"))[:60]} for r in rows]

    rows = list(tx.query('match $m isa security_measure; '
                         'fetch { "e": $m.ent-key };').resolve())
    ent = [str(r.get("e"))[:60] for r in rows]
    out["measures_total"] = len(ent)
    out["measures_distinct_ent_key"] = len(set(ent))
    out["measures_key_sample"] = Counter(ent).most_common(3)

    rows = list(tx.query('match $r isa security_requirement; '
                         'fetch { "k": $r.requirement_key };').resolve())
    req = [str(r.get("k"))[:60] for r in rows]
    out["requirements_total"] = len(req)
    out["requirements_distinct_key"] = len(set(req))

print(json.dumps(out, ensure_ascii=False, indent=1))
