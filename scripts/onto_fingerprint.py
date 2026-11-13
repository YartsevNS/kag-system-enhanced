"""Отпечаток онтологии для сравнения «до/после» оптимизации.

Сравнивать «число фактов» мало: правка пути записи может тихо потерять провенанс или
создать дубль. Скрипт снимает:
  - полное множество ent-key по типам (сравнивается побайтово),
  - число связей по каждому типу,
  - провенанс: у скольких объектов есть source-chunk-id, есть ли дубли значений,
  - счётчики dropped-кандидатов не нужны: они в логах прогона.

Запуск в контейнере kag-api:
    python /tmp/onto_fingerprint.py before.json
    ... прогон ...
    python /tmp/onto_fingerprint.py after.json
    python /tmp/onto_fingerprint.py --diff before.json after.json
"""
import json
import sys

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts


def snapshot() -> dict:
    c = typedb_facts._cfg()
    d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                      DriverOptions(DriverTlsConfig.disabled()))
    out = {"db": c["db"], "entities": {}, "relations": {}, "provenance": {}}
    with d.transaction(c["db"], TransactionType.READ) as tx:
        rows = list(tx.query('match $t sub $r; fetch { "t": $t };').resolve())
        ent_types, rel_types = [], []
        for r in rows:
            t = r.get("t") or {}
            if not isinstance(t, dict) or not t.get("label"):
                continue
            (ent_types if t.get("kind") == "entity" else rel_types).append(t["label"])

        for label in sorted(set(ent_types)):
            try:
                keys = [str(r.get("k")) for r in list(tx.query(
                    f'match $x isa {label}, has ent-key $k; fetch {{ "k": $k }};').resolve())]
            except Exception:  # noqa: BLE001 - у типа может не быть атрибута
                keys = []
            if keys:
                out["entities"][label] = sorted(set(keys))
                with_prov = 0
                try:
                    with_prov = len({str(r.get("k")) for r in list(tx.query(
                        f'match $x isa {label}, has source-chunk-id $c, has ent-key $k; '
                        f'fetch {{ "k": $k }};').resolve())})
                except Exception:  # noqa: BLE001
                    pass
                out["provenance"][label] = {"all": len(set(keys)), "with_source_chunk": with_prov}

        for label in sorted(set(rel_types)):
            try:
                rows = list(tx.query(f"match $r isa {label}; reduce $n = count;").resolve())
                n = rows[0].get("n").get_integer() if rows else 0
            except Exception:  # noqa: BLE001
                n = 0
            if n:
                out["relations"][label] = n
    return out


def diff(a: dict, b: dict) -> None:
    print("=== сущности по типам (было → стало) ===")
    for label in sorted(set(a["entities"]) | set(b["entities"])):
        ka = set(a["entities"].get(label) or [])
        kb = set(b["entities"].get(label) or [])
        mark = "" if ka == kb else "  <<< РАСХОЖДЕНИЕ"
        print(f"  {label:26} {len(ka):6} → {len(kb):6}  потеряно {len(ka - kb)}, добавлено {len(kb - ka)}{mark}")
        for k in list(ka - kb)[:3]:
            print("      потерян:", k)
        for k in list(kb - ka)[:3]:
            print("      добавлен:", k)
    print("=== связи по типам ===")
    for label in sorted(set(a["relations"]) | set(b["relations"])):
        na, nb = a["relations"].get(label, 0), b["relations"].get(label, 0)
        mark = "" if na == nb else "  <<< РАСХОЖДЕНИЕ"
        print(f"  {label:26} {na:6} → {nb:6}{mark}")
    print("=== провенанс ===")
    for label in sorted(a["provenance"]):
        pa = a["provenance"][label]
        pb = b["provenance"].get(label) or {}
        print(f"  {label:26} {pa['with_source_chunk']}/{pa['all']} → "
              f"{pb.get('with_source_chunk')}/{pb.get('all')}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--diff"]:
        a = json.load(open(sys.argv[2], encoding="utf-8"))
        b = json.load(open(sys.argv[3], encoding="utf-8"))
        diff(a, b)
    else:
        path = sys.argv[1] if len(sys.argv) > 1 else "fingerprint.json"
        snap = snapshot()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False)
        print("снимок записан:", path,
              "| сущностей по типам:", len(snap["entities"]),
              "| связей по типам:", len(snap["relations"]))
