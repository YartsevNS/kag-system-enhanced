"""Удаление следов документа из TypeDB по провенансу (source-chunk-id).

Зачем отдельный скрипт: `typedb_cleanup.delete_document_ontology` ищет чанки шаблоном
`"<doc>.*"`, а у нас chunk_id вида `<doc>-chunk_00001`, поэтому документ и элементы
не находились (счётчики 0). Здесь отбор по префиксу, затем снятие связей по ролям
из живой схемы и удаление объектов.

Порядок: связи → сущности → версия → документ. Каждый шаг — своя транзакция.
"""
import json
import sys

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

from src.indexing import typedb_facts

DOC = sys.argv[1] if len(sys.argv) > 1 else "e11fd1cf-87c1-4b49-8a94-ff8e4a3a4808"
DRY = "--dry-run" in sys.argv
PREFIX = DOC[:8]

c = typedb_facts._cfg()
drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled()))


def q_read(query):
    with drv.transaction(c["db"], TransactionType.READ) as tx:
        return list(tx.query(query).resolve())


def q_write(query):
    with drv.transaction(c["db"], TransactionType.WRITE) as tx:
        tx.query(query).resolve()
        tx.commit()


# схема: типы связей и их роли
rows = q_read('match $t sub $r; fetch { "t": $t };')
rel_roles = {}
for r in rows:
    t = r.get("t") or {}
    if isinstance(t, dict) and t.get("kind") == "relation" and t.get("label"):
        rel_roles.setdefault(t["label"], [])

# роли по типам связей
for label in list(rel_roles):
    try:
        rr = q_read(f'match $t {label}; fetch {{ "t": $t }};')
    except Exception:
        rr = []
    try:
        # роли достаём запросом по схеме через сервис онтологии
        from src.api.services.ontology_service import ontology_service

        schema = ontology_service.get_schema() or {}
        break
    except Exception:
        schema = {}

roles_map = (schema.get("roles") or {}) if isinstance(schema, dict) else {}

ents = {}
for label in ("structural_element", "security_requirement", "security_measure"):
    try:
        rows = q_read(f'match $x isa {label}, has source-chunk-id $c, has ent-key $k; '
                      f'fetch {{ "c": $c, "k": $k }};')
    except Exception as exc:
        print(label, "чтение не удалось:", str(exc)[:100])
        continue
    keys = sorted({str(r.get("k")) for r in rows if str(r.get("c")).startswith(PREFIX)})
    if keys:
        ents[label] = keys
        print(f"{label}: найдено {len(keys)}")

if DRY:
    print("dry-run: ничего не удалено; роли из схемы:", len(roles_map))
    raise SystemExit(0)

deleted_links = 0
own_types = set(ents)
for rel, roles in roles_map.items():
    if not isinstance(roles, list):
        continue
    for role in roles:
        for label, keys in ents.items():
            # связь, где наша сущность играет эту роль — удаляем целиком
            for key in keys[:200]:
                try:
                    q_write(f'match $x isa {label}, has ent-key "{key}"; '
                            f'$r isa {rel} ({role}: $x); delete $r;')
                    deleted_links += 1
                except Exception:
                    continue

print("связей снято (попыток):", deleted_links)

deleted = 0
for label, keys in ents.items():
    for key in keys:
        try:
            q_write(f'match $x isa {label}, has ent-key "{key}"; delete $x;')
            deleted += 1
        except Exception as exc:
            print("не удалён", label, key[:40], str(exc)[:80])
print("сущностей удалено:", deleted)

# версия и документ
try:
    q_write(f'match $v isa document_version, has kag_document_id "{DOC}"; '
            f'$r isa document_has_version (version: $v); delete $r;')
except Exception:
    pass
try:
    q_write(f'match $v isa document_version, has kag_document_id "{DOC}"; delete $v;')
    print("версия удалена")
except Exception as exc:
    print("версия не удалена:", str(exc)[:100])

try:
    q_write(f'match $d isa regulatory_document, has document_key "kag:{DOC}"; '
            f'$i isa document_issuance (regulated_item: $d); delete $i;')
except Exception:
    pass
try:
    q_write(f'match $d isa regulatory_document, has document_key "kag:{DOC}"; delete $d;')
    print("документ удалён")
except Exception as exc:
    print("документ не удалён:", str(exc)[:120])

left = q_read('match $x isa structural_element, has source-chunk-id $c; fetch { "c": $c };')
print("осталось элементов с этим префиксом:",
      sum(1 for r in left if str(r.get("c")).startswith(PREFIX)))
