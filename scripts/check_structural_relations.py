"""Проверка структурных связей: не потерял ли их новый путь записи.

Структурные связи детерминированы (документ → редакция → пункты, требование → пункт),
поэтому их число сравнимо между прогонами: document_issuance = 1, document_has_version = 1,
current_version_of = 1, document_structure ≈ число структурных элементов,
requirement_derived_from ≈ число требований.
"""
import json

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))

checks = ["document_issuance", "document_has_version", "current_version_of",
          "document_structure", "requirement_derived_from", "measure_source",
          "requirement_definition", "supersedes"]
out = {}
with d.transaction(c["db"], TransactionType.READ) as tx:
    for label in checks:
        try:
            rows = list(tx.query(f"match $r isa {label}; reduce $n = count;").resolve())
            out[label] = rows[0].get("n").get_integer() if rows else 0
        except Exception as exc:  # noqa: BLE001
            out[label] = f"ERR {str(exc)[:60]}"
    for label in ("structural_element", "security_requirement", "security_measure"):
        rows = list(tx.query(f"match $x isa {label}; reduce $n = count;").resolve())
        out["#" + label] = rows[0].get("n").get_integer() if rows else 0
print(json.dumps(out, ensure_ascii=False, indent=1))
