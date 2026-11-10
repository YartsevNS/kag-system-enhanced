"""Полное удаление графа документа из TypeDB по схеме (все связи, затем сущности).

Штатная очистка продукта (typedb_cleanup) ищет по source-chunk-id и в части случаев
не находит объекты. Здесь действуем иначе: собираем сущности документа по связям
(версия → пункт → требование), затем для каждой сущности снимаем ВСЕ связи, где она
участвует (роли берём из схемы), и только потом удаляем сами сущности.
"""
import sys

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

DOC_IDS = sys.argv[1:] or [
    "1bcca7c7-aeb5-481d-885c-12dc388b5ef2",
    "f41fb447-e9ae-47d7-bd14-a347686f3790",
]

c = typedb_facts._cfg()
drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                    DriverOptions(DriverTlsConfig.disabled()))


def q_escape(v: str) -> str:
    return (v or "").replace('"', '\\"')


def rel_roles() -> dict:
    schema = drv.databases.get(c["db"]).schema()
    out, cur = {}, None
    for line in schema.replace("\r", "").split("\n"):
        line = line.strip().rstrip(",")
        if line.startswith("relation "):
            name = line.split()[1].rstrip(",").rstrip(";")
            out.setdefault(name, set())
            cur = name
        elif line.startswith("relates ") and cur:
            out[cur].add(line.split()[1].rstrip(";"))
        elif line.startswith(("entity ", "attribute ", "fun ", "define", "}")) or line == "":
            cur = None if not line.startswith("relates") else cur
    return {k: sorted(v) for k, v in out.items() if v}


REL = rel_roles()
print("связей в схеме:", len(REL))


def run(query: str) -> bool:
    try:
        with drv.transaction(c["db"], TransactionType.WRITE) as tx:
            tx.query(query).resolve()
            tx.commit()
        return True
    except Exception:  # noqa: BLE001
        return False


def collect(doc_id: str):
    """ent-key версии, документа, пунктов и требований."""
    vers, docs, elems, reqs = [], [], [], []
    with drv.transaction(c["db"], TransactionType.READ) as tx:
        def vals(query, field):
            out = []
            for r in list(tx.query(query).resolve()):
                x = r.get(field)
                v = (x.get("@value") if isinstance(x, dict) else x) or ""
                if v:
                    out.append(v)
            return out

        base = ("match $v isa document_version, has kag_document_id "
                f'"{q_escape(doc_id)}"; ')
        vers = vals(base + 'fetch {"k": $v.ent-key};', "k")
        docs = vals(base + "document_has_version(version: $v, document: $d); "
                    'fetch {"k": $d.ent-key};', "k")
        elems = vals(base + "document_structure(version: $v, element: $e); "
                     'fetch {"k": $e.ent-key};', "k")
        reqs = vals(base + "document_structure(version: $v, element: $e); "
                    "requirement_derived_from(clause: $e, requirement: $rq); "
                    'fetch {"k": $rq.ent-key};', "k")
    return vers, docs, elems, reqs


for doc_id in DOC_IDS:
    vers, docs, elems, reqs = collect(doc_id)
    print(f"{doc_id}: версий {len(vers)}, документов {len(docs)}, пунктов {len(elems)}, требований {len(reqs)}")
    entities = [("document_version", k) for k in vers] + [("document", k) for k in docs] + \
               [("structural_element", k) for k in elems] + [("security_requirement", k) for k in reqs]

    # 1) снять все связи, где участвует хоть одна сущность документа
    for etype, key in entities:
        for rel, roles in REL.items():
            for role in roles:
                run(f'match $x isa {etype}, has ent-key "{q_escape(key)}"; '
                    f'$r isa {rel}; $r links ({role}: $x); delete $r;')

    # 2) удалить сущности: сначала требования, потом пункты, версия, документ
    for etype in ("security_requirement", "structural_element", "document_version", "document"):
        for t, key in entities:
            if t != etype:
                continue
            run(f'match $x isa {etype}, has ent-key "{q_escape(key)}"; delete $x;')

    left = collect(doc_id)
    print("  после удаления:", [len(x) for x in left])

drv.close()
