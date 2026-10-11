"""Перевод типов связей в графе на канонические коды + схлопывание дублей.

Зачем. Словарь переведён на канонические коды (component-of, related-to, signed-by …), и новые связи
пишутся правильно. Но в графе остались связи со ПРЕЖНИМИ написаниями (PART_OF, RELATED_TO, SIGNED_BY,
HAS_CLAUSE …), потому что пересборка чистит только узел документа и его фрагменты, а связи между
сущностями висят на самих сущностях и переживают пересборку. В итоге одна и та же пара получает два
написания типа — то есть дубль.

Что делает прибор:
  1. dry-run (по умолчанию): показывает, сколько связей каждого прежнего типа и сколько даст дублей;
  2. --apply: переименовывает тип через apoc.refactor.setType (если доступен) либо создаёт новую связь
     с каноническим типом и удаляет старую;
  3. после переименования схлопывает дубли: для одинаковых (источник, тип, цель) оставляет одну связь,
     перенося в неё отсутствующие свойства (происхождение, фрагмент);
  4. печатает контрольный замер: сколько прежних типов осталось (должно быть 0) и сколько дублей.

Запуск в контейнере api:
    docker exec kag-api python /app/data/migrate_relation_types.py            # предпросмотр
    docker exec kag-api python /app/data/migrate_relation_types.py --apply    # применить
"""
import sys

from neo4j import GraphDatabase

sys.path.insert(0, "/app")
try:
    from src.indexing.graph_ontology import MIGRATION
except Exception as exc:  # noqa: BLE001
    print("не удалось прочитать перевод типов:", exc)
    raise SystemExit(2)

APPLY = "--apply" in sys.argv
SEM = ("NOT type(r) IN ['MENTIONS','HAS_CHUNK','HAS_SECTION','SECTION_CHUNK','HAS_SUBORDINATE',"
       "'HAS_COMPONENT','HAS_MEMBER']")

drv = GraphDatabase.driver(
    __import__("os").environ.get("NEO4J_URI") or "bolt://neo4j:7687",
    auth=(__import__("os").environ.get("NEO4J_USER", "neo4j"),
          __import__("os").environ.get("NEO4J_PASSWORD", "")),
)


def apoc_available(session) -> bool:
    try:
        row = session.run("RETURN apoc.version() AS v").single()
        return bool(row and row["v"])
    except Exception:
        return False


def main() -> int:
    # Переводим только те прежние коды, которые ОТЛИЧАЮТСЯ от канонических (иначе это no-op).
    # Переводим ТОЛЬКО реальные переименования: где ключ перевода отличается от канонического кода.
    # Ключи, совпадающие со значением (канон сам на себя), не трогаем — иначе прибор переименовывал бы
    # то, что уже правильно.
    mapping = {old: new for old, new in MIGRATION.items() if str(old) != str(new)}
    print(f"прежних написаний к переводу: {len(mapping)}")

    with drv.session() as s:
        use_apoc = apoc_available(s)
        print("APOC доступен:", use_apoc)

        print("\n=== до правки: что лежит в графе ===")
        total_old = 0
        for old, new in sorted(mapping.items()):
            cnt = s.run(f"MATCH ()-[r:`{old}`]->() RETURN count(r) AS n").single()["n"]
            if cnt:
                total_old += cnt
                print(f"  {old:<14} → {new:<24} {cnt}")
        print(f"  всего связей с прежними типами: {total_old}")

        if not APPLY:
            print("\nпредпросмотр. Для применения добавьте --apply")
            drv.close()
            return 0

        print("\n=== переименование ===")
        renamed = 0
        for old, new in sorted(mapping.items()):
            cnt = s.run(f"MATCH ()-[r:`{old}`]->() RETURN count(r) AS n").single()["n"]
            if not cnt:
                continue
            if use_apoc:
                s.run(f"MATCH ()-[r:`{old}`]->() CALL apoc.refactor.setType(r, '{new}') YIELD input, output "
                      "RETURN count(*)")
            else:
                # Без APOC: создаём связь с каноническим типом, переносим свойства, удаляем старую.
                s.run(f"""
                    MATCH (a)-[r:`{old}`]->(b)
                    CREATE (a)-[n:`{new}`]->(b)
                    SET n = properties(r)
                    DELETE r
                """)
            renamed += cnt
            print(f"  {old:<14} → {new:<24} {cnt}")
        print(f"  переименовано: {renamed}")

        print("\n=== схлопывание дублей (одинаковые источник, тип, цель) ===")
        # Оставляем связь с наибольшей полнотой происхождения (есть версия схемы и фрагмент),
        # в остальные — переносим недостающие свойства, затем удаляем лишние.
        res = s.run("""
            MATCH (a)-[r]->(b)
            WHERE NOT type(r) IN ['MENTIONS','HAS_CHUNK','HAS_SECTION','SECTION_CHUNK']
            WITH a, b, type(r) AS t, collect(r) AS rs
            WHERE size(rs) > 1
            WITH a, b, t, rs,
                 [x IN rs WHERE x.schema_version IS NOT NULL] AS withv
            WITH a, b, t, rs, CASE WHEN size(withv) > 0 THEN withv[0] ELSE rs[0] END AS keep,
                 [x IN rs WHERE x <> CASE WHEN size(withv) > 0 THEN withv[0] ELSE rs[0] END] AS drop
            FOREACH (d IN drop | SET keep.chunk_id = coalesce(keep.chunk_id, d.chunk_id),
                                 keep.doc_id   = coalesce(keep.doc_id, d.doc_id),
                                 keep.layer    = coalesce(keep.layer, d.layer))
            FOREACH (d IN drop | DELETE d)
            RETURN count(*) AS merged
        """).single()
        print("  схлопнуто групп дублей:", res["merged"] if res else 0)

        print("\n=== после правки ===")
        left = s.run(f"MATCH ()-[r]->() WHERE {SEM} RETURN count(r) AS vsego, "
                     "count(CASE WHEN r.schema_version IS NULL THEN 1 END) AS b_schema, "
                     "count(CASE WHEN r.chunk_id IS NULL OR r.chunk_id='' THEN 1 END) AS b_chunk").single()
        print(f"  смысловых связей: {left['vsego']} | без версии схемы: {left['b_schema']} | без фрагмента: {left['b_chunk']}")
        rest = 0
        for old in mapping:
            cnt = s.run(f"MATCH ()-[r:`{old}`]->() RETURN count(r) AS n").single()["n"]
            rest += cnt
        print(f"  связей с прежними типами осталось: {rest} (должно быть 0)")
    drv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
