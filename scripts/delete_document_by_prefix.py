"""Удаление следов документа из TypeDB по провенансу (source-chunk-id).

Почему не `typedb_cleanup`: он ищет чанки шаблоном `<doc>.*`, а у нас chunk_id вида
`<doc>-chunk_00001` — поэтому документ и элементы не находились (счётчики 0).
Здесь отбор регулярным `like "<prefix>.*"` (проверено: находит 70 элементов), затем
снятие связей по ролям из живой схемы и удаление объектов.

Порядок: связи → сущности → версия → документ. Каждый шаг — своя транзакция.
"""
import json
import sys

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

DOC = sys.argv[1] if len(sys.argv) > 1 else "e11fd1cf-87c1-4b49-8a94-ff8e4a3a4808"
DRY = "--dry-run" in sys.argv
PREFIX = DOC[:8]
OWN = ("structural_element", "security_requirement", "security_measure")

c = typedb_facts._cfg()
drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                    DriverOptions(DriverTlsConfig.disabled()))


def q_read(query):
    with drv.transaction(c["db"], TransactionType.READ) as tx:
        return list(tx.query(query).resolve())


def q_write(query):
    with drv.transaction(c["db"], TransactionType.WRITE) as tx:
        tx.query(query).resolve()
        tx.commit()


roles_map = {}
try:
    from src.api.services.ontology_service import ontology_service

    sch = ontology_service.get_schema() or {}
    for rel in sch.get("relations") or []:
        if isinstance(rel, dict) and rel.get("label"):
            roles_map[rel["label"]] = list(rel.get("roles") or [])
except Exception as exc:  # noqa: BLE001
    print("схема ролей не прочитана:", str(exc)[:120])

counts = {}
for label in OWN:
    rows = q_read(f'match $x isa {label}, has source-chunk-id $c; $c like "{PREFIX}.*"; '
                  f'reduce $n = count;')
    counts[label] = rows[0].get("n").get_integer() if rows else 0
print("к удалению:", json.dumps(counts, ensure_ascii=False), "| ролей в схеме:", len(roles_map))
if DRY:
    raise SystemExit(0)

links = 0
for rel, roles in roles_map.items():
    if not isinstance(roles, list):
        continue
    for role in roles:
        for label in OWN:
            try:
                q_write(f'match $x isa {label}, has source-chunk-id $c; $c like "{PREFIX}.*"; '
                        f'$r isa {rel} ({role}: $x); delete $r;')
                links += 1
            except Exception:  # noqa: BLE001 - нет такой комбинации тип/роль
                continue
print("шагов снятия связей:", links)

for label in OWN:
    try:
        q_write(f'match $x isa {label}, has source-chunk-id $c; $c like "{PREFIX}.*"; delete $x;')
        print("удалены объекты:", label)
    except Exception as exc:  # noqa: BLE001
        print("не удалено", label, str(exc)[:120])

for q, title in (
    (f'match $v isa document_version, has kag_document_id "{DOC}"; '
     f'$r isa document_has_version (version: $v); delete $r;', "связь версии"),
    (f'match $v isa document_version, has kag_document_id "{DOC}"; delete $v;', "версия"),
    (f'match $d isa regulatory_document, has document_key "kag:{DOC}"; '
     f'$i isa document_issuance (regulated_item: $d); delete $i;', "связь выдачи"),
    (f'match $d isa regulatory_document, has document_key "kag:{DOC}"; delete $d;', "документ"),
):
    try:
        q_write(q)
        print("удалено:", title)
    except Exception as exc:  # noqa: BLE001
        print("не удалено:", title, str(exc)[:100])

left = q_read(f'match $x isa structural_element, has source-chunk-id $c; '
              f'$c like "{PREFIX}.*"; reduce $n = count;')
print("осталось элементов с этим префиксом:",
      left[0].get("n").get_integer() if left else 0)
