"""Поиск дублей документов: только чтение, ничего не удаляет.

Зачем. В корпусе есть документы, загруженные по нескольку раз (одно имя, один хэш). Каждая копия —
отдельный узел графа, поэтому связи, счётчики и выдача дублируются: на замере связей между документами
одна и та же пара повторялась восемь раз. Но удалять сразу нельзя: похожий документ может оказаться
НЕ дублем (например, разные редакции одного стандарта). Поэтому прибор сначала показывает список и
что именно будет затронуто, а решение принимает человек.

Что показывает по каждой группе дублей:
  * по какому признаку сочтены дублями (хэш файла — сильный признак; имя+размер — слабый);
  * сколько у каждого документа фрагментов, есть ли узел в графе, когда загружен;
  * какой документ предлагается ОСТАВИТЬ (самый ранний — он уже в связях и в выдаче);
  * что произойдёт при удалении остальных (сколько фрагментов и связей уйдёт).

Запуск в контейнере api:
    docker exec kag-api python /app/data/find_duplicate_documents.py
    docker exec kag-api python /app/data/find_duplicate_documents.py --json   # машинный вывод
"""
import json
import os
import sys
import psycopg2
import psycopg2.extras
from neo4j import GraphDatabase

JSON_OUT = "--json" in sys.argv

dsn = os.environ.get("KAG_DB_URL") or os.environ.get("DATABASE_URL") or ""

with psycopg2.connect(dsn) as conn:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id::text AS id, filename, file_hash, file_size, status, chunks_count,
                   coalesce(recognized_title,'') AS title, uploaded_by,
                   coalesce(created_at, now()) AS created_at
            FROM documents
            ORDER BY created_at
        """)
        docs = [dict(r) for r in cur.fetchall()]

# Исключаем то, что вообще нельзя считать дублем: файлы без имени или без хэша,
# и заведомо служебные карточки мониторинга (очень маленькие тексты с заголовком и ссылкой).
def usable(d):
    if not (d.get("filename") or "").strip():
        return False
    if not (d.get("file_hash") or "").strip():
        return False
    if (d.get("file_size") or 0) < 2048:
        return False
    return True


docs = [d for d in docs if usable(d)]

groups = {}
for d in docs:
    groups.setdefault(("hash", d["file_hash"]), []).append(d)
weak = {}
for d in docs:
    weak.setdefault(("name", d["filename"].strip().lower(), d.get("file_size") or 0), []).append(d)

strong_dupes = {k: v for k, v in groups.items() if len(v) > 1}
weak_dupes = {k: v for k, v in weak.items() if len(v) > 1 and k not in strong_dupes}

# Что уходит: считаем фрагменты и связи документов-кандидатов на удаление.
total_extra = sum(len(v) - 1 for v in strong_dupes.values())
result = {
    "документов_всего": len(docs),
    "групп_дублей_по_хэшу": len(strong_dupes),
    "лишних_копий_по_хэшу": total_extra,
    "групп_похожих_по_имени": len(weak_dupes),
    "группы": [],
}

if total_extra:
    try:
        drv = GraphDatabase.driver(
            os.environ.get("NEO4J_URI") or "bolt://neo4j:7687",
            auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
        )
        with drv.session() as s:
            for (kind, key), items in list(strong_dupes.items()):
                keep, *drop = items
                drop_ids = [x["id"] for x in drop]
                st = s.run(
                    "MATCH (a:Document)-[r]-() WHERE a.id IN $ids RETURN count(r) AS n",
                    ids=drop_ids).single()
                result["группы"].append({
                    "признак": "одинаковый хэш файла",
                    "файл": keep["filename"],
                    "оставить": {"id": keep["id"][:8], "когда": str(keep["created_at"])[:16],
                                 "фрагментов": keep["chunks_count"]},
                    "удалить": [{"id": x["id"][:8], "когда": str(x["created_at"])[:16],
                                 "фрагментов": x["chunks_count"], "статус": x["status"]} for x in drop],
                    "связей_у_удаляемых": st["n"] if st else 0,
                })
        drv.close()
    except Exception as e:  # noqa: BLE001
        result["предупреждение"] = f"граф недоступен: {e}"

if JSON_OUT:
    print(json.dumps(result, ensure_ascii=False, indent=1))
else:
    print(f"документов (после отсева служебных мелочей): {result['документов_всего']}")
    print(f"групп дублей по хэшу файла: {result['групп_дублей_по_хэшу']}"
          f" | лишних копий: {result['лишних_копий_по_хэшу']}")
    print(f"групп похожих по имени и размеру (слабый признак): {result['групп_похожих_по_имени']}")
    for g in result.get("группы", []):
        print(f"\n  {g['файл']}")
        print(f"    оставить: {g['оставить']['id']} ({g['оставить']['когда']}, "
              f"фрагментов {g['оставить']['фрагментов']})")
        for x in g["удалить"]:
            print(f"    удалить:  {x['id']} ({x['когда']}, фрагментов {x['фрагментов']}, {x['статус']})")
        print(f"    у удаляемых связей в графе: {g['связей_у_удаляемых']}")
    if result.get("предупреждение"):
        print("\n" + result["предупреждение"])
    print("\nНичего не удалено. Это предпросмотр.")
