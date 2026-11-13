"""Счётчики объектов онтологии по типам — для замера повторяемости прогонов."""
import json

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))
types = ("security_measure", "security_requirement", "structural_element",
         "regulatory_document", "document_version", "regulator")
out = {}
with d.transaction(c["db"], TransactionType.READ) as tx:
    for t in types:
        rows = list(tx.query(f"match $x isa {t}; reduce $n = count;").resolve())
        out[t] = rows[0].get("n").get_integer() if rows else 0
    rel = list(tx.query('match $t sub $r; fetch { "t": $t };').resolve())
labels = []
for r in rel:
    t = r.get("t") or {}
    if isinstance(t, dict) and t.get("kind") == "relation" and t.get("label"):
        labels.append(t["label"])
total_rel = 0
with d.transaction(c["db"], TransactionType.READ) as tx:
    for label in labels:
        try:
            rows = list(tx.query(f"match $r isa {label}; reduce $n = count;").resolve())
            if rows:
                total_rel += rows[0].get("n").get_integer() or 0
        except Exception:  # noqa: BLE001
            continue
out["relations"] = total_rel
print(json.dumps(out, ensure_ascii=False))
