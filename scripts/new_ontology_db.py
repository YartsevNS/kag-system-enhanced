"""Чистая база онтологии под проверку одного документа.

Зачем: текущая cybersecurity_ontology_v4 уже засорена результатами прогонов до починки
ключей (229 «мер», часть из них — заголовки направлений; требования с ключами от старого
document_key). Удалять их поштучно нельзя: TypeDB держит связи, и удаление падает на
@card(1..1) (CNT5). Чистая база — единственный способ увидеть настоящий результат
исправленного извлечения, не рискуя рабочей базой.

Старая база остаётся как есть (история), имя базы задаётся аргументом.

Запуск внутри kag-api:
    python /tmp/new_ontology_db.py cybersecurity_ontology_v5
"""
import sys

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

DB = sys.argv[1] if len(sys.argv) > 1 else "cybersecurity_ontology_v5"

c = typedb_facts._cfg()
drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))

existing = [db.name for db in drv.databases.all()]
print("базы до:", existing)
if DB in existing:
    print("база уже есть:", DB)
else:
    drv.databases.create(DB)
    print("создана:", DB)

schema_files = ["/app/src/ontology/schema-v3.tql", "/app/src/ontology/functions-v3.tql"]
for path in schema_files:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # TypeDB принимает define-блок целиком одной транзакцией
    with drv.transaction(DB, TransactionType.SCHEMA) as tx:
        tx.query(text).resolve()
        tx.commit()
    print("загружено:", path, len(text), "символов")

with drv.transaction(DB, TransactionType.READ) as tx:
    rows = list(tx.query('match $t sub $r; fetch { "t": $t };').resolve())
kinds = {}
for r in rows:
    t = r.get("t") or {}
    if isinstance(t, dict) and t.get("label"):
        kinds[t.get("kind", "?")] = kinds.get(t.get("kind", "?"), 0) + 1
print("типы в новой базе:", kinds)
