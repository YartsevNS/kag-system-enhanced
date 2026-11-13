"""Итог прогона одного документа: что реально легло в онтологию.

Проверяем не «процесс завершился», а качество данных:
- сколько мер защиты и у скольких заполнен код (АВЗ, ОПС, ЗНИ);
- есть ли у объектов провенанс (source-chunk-id) — без него факт недоказателен;
- какие документы представлены в онтологии;
- есть ли новые записи в журнале ошибок онтологии.

Только чтение.
"""
import json

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))


def rows(tx, q):
    return list(tx.query(q).resolve())


out = {}
with d.transaction(c["db"], TransactionType.READ) as tx:
    try:
        r = rows(tx, 'match $m isa security_measure, has code $k; fetch { "k": $k };')
        codes = [str(x.get("k")) for x in r]
        out["measures_with_code"] = len(codes)
        out["codes_sample"] = codes[:12]
    except Exception as e:  # noqa: BLE001
        out["measures_with_code"] = f"ERR {type(e).__name__}: {str(e)[:150]}"

    try:
        r = rows(tx, 'match $m isa security_measure; reduce $n = count;')
        out["measures_total"] = r[0].get("n").get_integer() if r else 0
    except Exception as e:  # noqa: BLE001
        out["measures_total"] = f"ERR {str(e)[:120]}"

    try:
        r = rows(tx, 'match $s isa security_requirement, has source-chunk-id $p; '
                     'reduce $n = count;')
        out["requirements_with_chunk"] = r[0].get("n").get_integer() if r else 0
    except Exception as e:  # noqa: BLE001
        out["requirements_with_chunk"] = f"ERR {str(e)[:120]}"

    try:
        r = rows(tx, 'match $s isa security_requirement; reduce $n = count;')
        out["requirements_total"] = r[0].get("n").get_integer() if r else 0
    except Exception as e:  # noqa: BLE001
        out["requirements_total"] = f"ERR {str(e)[:120]}"

    try:
        r = rows(tx, 'match $d isa regulatory_document, has title $t; fetch { "t": $t };')
        out["documents"] = [str(x.get("t"))[:70] for x in r][:8]
    except Exception as e:  # noqa: BLE001
        out["documents"] = f"ERR {str(e)[:120]}"

print(json.dumps(out, ensure_ascii=False, indent=1))

from sqlalchemy import text  # noqa: E402

from src.database.session import get_session_local  # noqa: E402

db = get_session_local()()
try:
    n = db.execute(text("select count(*) from ontology_errors where not resolved")).scalar()
    print("открытых записей журнала ошибок:", n)
finally:
    db.close()
