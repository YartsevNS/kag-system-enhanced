"""Уборка дублей документа: методический документ ФСТЭК «Меры защиты информации в ГИС».

Что случилось: ключ документа у записей без номера строился из названия целиком, включая
скобочное «(утв. 11.02.2014)» / «(утв. 11 февраля 2014 г.)». Из-за разного написания даты
один документ лёг в базу двумя объектами regulatory_document, и меры привязались к обоим.

Что делает скрипт (по шагам, каждый — своя транзакция):
1. находит regulatory_document, у которых ключ содержит «методический документ фстэк россии
   меры защиты информации»;
2. удаляет связи, где они участвуют (document_issuance, document_has_version,
   current_version_of, supersedes и прочие связи документа) — иначе TypeDB не даст удалить;
3. удаляет сами объекты;
4. печатает, что осталось.

Запуск с --dry-run — только показать, что будет удалено.
"""
import sys

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

DRY = "--dry-run" in sys.argv
NEEDLE = "методический документ фстэк россии меры защиты информации"

c = typedb_facts._cfg()
d = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))

# 1. находим объекты
with d.transaction(c["db"], TransactionType.READ) as tx:
    rows = list(tx.query(
        'match $d isa regulatory_document, has document_key $k; '
        'fetch { "k": $k };').resolve())
keys = sorted({str(r.get("k")) for r in rows if NEEDLE in str(r.get("k")).lower()})

print("найдено записей документа:", len(keys))
for k in keys:
    print(" -", k)

if not keys:
    raise SystemExit("нечего чистить")

if DRY:
    print("dry-run: изменения не применяются")
    raise SystemExit(0)

# 2. снимаем связи, где документ участвует
REL_QUERIES = [
    'match $rel isa document_issuance (regulated_item: $d);',
    'match $rel isa document_has_version (document: $d);',
    'match $rel isa current_version_of (document: $d);',
    'match $rel isa supersedes (successor: $d);',
    'match $rel isa supersedes (predecessor: $d);',
    'match $rel isa document_structure (whole: $d);',
    'match $rel isa document_hierarchy (parent: $d);',
]
for key in keys:
    for base in REL_QUERIES:
        q = (f'match $d isa regulatory_document, has document_key "{key}"; ' + base[6:]
             + ' delete $rel;')
        try:
            with d.transaction(c["db"], TransactionType.WRITE) as tx:
                tx.query(q).resolve()
                tx.commit()
        except Exception as e:  # noqa: BLE001
            print("  связь не снята:", base.split()[3], "->", str(e)[:90])

# 3. удаляем объекты
for key in keys:
    try:
        with d.transaction(c["db"], TransactionType.WRITE) as tx:
            tx.query(
                f'match $d isa regulatory_document, has document_key "{key}"; delete $d;'
            ).resolve()
            tx.commit()
        print("удалён:", key[:70])
    except Exception as e:  # noqa: BLE001
        print("не удалён:", key[:70], "->", str(e)[:120])

# 4. что осталось
with d.transaction(c["db"], TransactionType.READ) as tx:
    rows = list(tx.query('match $d isa regulatory_document, has document_key $k; '
                         'fetch { "k": $k };').resolve())
left = sorted({str(r.get("k")) for r in rows})
print("осталось документов:", len(left))
for k in left:
    print(" -", k[:100])

# ── дополнительно: снять меры и требования этого документа ────────────────────
# Меры, у которых ключ начинался с названия документа, а кода не было, писались как
# объекты и при повторных прогонах плодились (229 «мер» вместо 114). Удаляем их вместе
# с требованиями этого документа, чтобы пересоздать по исправленному ключу.
if "--with-children" in sys.argv:
    for etype, attr in (("security_measure", "measure_key"),
                        ("security_requirement", "requirement_key")):
        try:
            with d.transaction(c["db"], TransactionType.READ) as tx:
                found = [str(r.get("v")) for r in list(tx.query(
                    f'match $e isa {etype}, has {attr} $v; fetch {{ "v": $v }};').resolve())]
            target = []
            for key in {f for f in found if NEEDLE in f.lower()}:
                target.append(key)
            deleted = 0
            for key in target:
                try:
                    with d.transaction(c["db"], TransactionType.WRITE) as tx:
                        tx.query(f'match $e isa {etype}, has {attr} "{key}"; delete $e;').resolve()
                        tx.commit()
                    deleted += 1
                except Exception as e:  # noqa: BLE001 - связи мешают удалению
                    print(f"  {etype} не удалён ({key[:50]}): {str(e)[:80]}")
            print(f"{etype}: найдено {len(target)}, удалено {deleted}")
        except Exception as e:  # noqa: BLE001
            print(f"{etype}: ошибка разбора: {str(e)[:120]}")
