"""Уборка онтологии: мусорные и дублирующиеся издатели + версии без документа.

Правила:
  * издатель, чьё название не похоже на орган (is_body=False), удаляется вместе со
    связями document_issuance;
  * из нескольких названий одного органа (по коду справочника) остаётся одно,
    остальные удаляются;
  * версия без связи document_has_version удаляется.

Запуск: python cleanup_junk.py [--apply]
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import issuers, typedb_facts

APPLY = "--apply" in sys.argv

c = typedb_facts._cfg()
drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                    DriverOptions(DriverTlsConfig.disabled()))


def run(q: str) -> bool:
    try:
        with drv.transaction(c["db"], TransactionType.WRITE) as tx:
            tx.query(q).resolve()
            tx.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        msg = " ".join(l for l in str(exc).splitlines() if l.strip())[:100]
        print("   пропуск:", msg)
        return False


try:
    with drv.transaction(c["db"], TransactionType.READ) as tx:
        regs = []
        for r in list(tx.query(
                'match $r isa regulator, has ent-key $k; '
                'fetch {"k": $k, "n": $r.name, "s": $r.short_name};').resolve()):
            def val(n):
                x = r.get(n)
                return (x.get("@value") if isinstance(x, dict) else x) or ""
            regs.append({"key": val("k"), "name": val("n"), "short": val("s")})

    print("издателей всего:", len(regs))
    bad = [r for r in regs if not issuers.is_body(r["name"], r["short"])]
    good = [r for r in regs if issuers.is_body(r["name"], r["short"])]

    # дубли по коду органа: оставляем первый
    keep, drop = {}, []
    for r in good:
        code = issuers.code_of(r["name"], r["short"]) or issuers.normalize(r["name"])
        if code in keep:
            drop.append(r)
        else:
            keep[code] = r

    print("мусорных:", len(bad), "| дублей:", len(drop))
    for r in bad:
        print("  мусор:", r["name"][:60], "|", r["short"][:20])
    for r in drop:
        print("  дубль:", r["name"][:60], "|", r["short"][:20])

    for r in bad + drop:
        if not APPLY:
            continue
        k = r["key"]
        run(f'match $r isa regulator, has ent-key "{k}"; '
            "$i isa document_issuance; $i links (issuer: $r); delete $i;")
        run(f'match $r isa regulator, has ent-key "{k}"; delete $r;')

    with drv.transaction(c["db"], TransactionType.READ) as tx:
        orphans = []
        for r in list(tx.query(
                'match $v isa document_version, has kag_document_id $id; '
                'not { document_has_version(version: $v, document: $d); }; '
                'fetch {"id": $id};').resolve()):
            x = r.get("id")
            orphans.append((x.get("@value") if isinstance(x, dict) else x) or "")
    print("версий без документа:", len(orphans))
    for doc_id in orphans:
        if not APPLY:
            continue
        run(f'match $v isa document_version, has kag_document_id "{doc_id}"; delete $v;')
        print("  удалена версия:", doc_id)
finally:
    drv.close()

print("режим:", "ПРИМЕНЕНО" if APPLY else "сухой прогон (--apply чтобы применить)")
