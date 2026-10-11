"""Снести СМЫСЛОВЫЕ связи документа (те, что извлечены из его фрагментов).

Зачем: связи в графе висят на СУЩНОСТЯХ (`(a)-[r]->(b)` между Entity), а не на узлах документа,
поэтому штатная чистка графа документа (`clear_document`) их НЕ удаляет: узлы-фрагменты уходят,
а связи остаются с прежним `chunk_id`. Из-за этого числа связей при повторных пересборках
накапливаются, и замер показывает смесь прогонов, а не результат одного (живой пример 11.10.2026:
551 → 1765 → 1894 → 1976 на одном и том же документе).

Прибор удаляет только связи, у которых `chunk_id` входит в фрагменты документа, и НЕ трогает
структурные (MENTIONS/HAS_CHUNK — их пишет код).

    docker exec kag-api python /tmp/clear_doc_relations.py <document_id>
"""

import sys

DOC = sys.argv[1] if len(sys.argv) > 1 else "2873e687-d476-43e6-b94e-f4dd14eaead8"

from src.indexing.knowledge_graph import kg_service  # noqa: E402

with kg_service.driver.session() as s:
    ids = [str(r["id"]) for r in s.run(
        "MATCH (d:Document {id: $doc})-[:HAS_CHUNK]->(c:Chunk) RETURN c.id AS id", doc=DOC)]
    if not ids:
        print("у документа нет узлов фрагментов в графе")
        raise SystemExit(0)
    rec = s.run(
        """MATCH (a)-[r]->(b)
           WHERE r.chunk_id IN $ids AND NOT type(r) IN ['MENTIONS', 'HAS_CHUNK', 'SECTION_CHUNK']
           DELETE r""",
        ids=ids).consume()
    print(f"фрагментов документа: {len(ids)} | удалено смысловых связей: "
          f"{rec.counters.relationships_deleted}")
