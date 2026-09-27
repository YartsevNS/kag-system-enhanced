"""Что лежит в векторном индексе по документу: текстовые фрагменты и табличные.

Зачем: проверить, что восстановленная таблица стала не только записью в табличном слое, но и
отдельным табличным фрагментом (chunk_type=table), то есть её видно в поиске и на странице «Чанки».

Запуск внутри контейнера api:
    docker exec -i kag-api python /app/data/check_doc_chunks.py <document_id>
"""
import os
import sys

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue


def main() -> int:
    doc_id = sys.argv[1]
    client = QdrantClient(url=os.environ.get("QDRANT_URL", "http://kag-qdrant:6333"),
                          api_key=os.environ.get("QDRANT_API_KEY") or None)
    points, _ = client.scroll(
        collection_name=os.environ.get("QDRANT_COLLECTION", "kag_documents"),
        limit=50, with_payload=True, with_vectors=False,
        scroll_filter=Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=doc_id))]),
    )
    print(f"  документ: {doc_id}")
    print(f"  фрагментов в индексе: {len(points)}")
    table_count = 0
    for p in points:
        payload = p.payload or {}
        meta = payload.get("metadata") or {}
        kind = meta.get("chunk_type", "text")
        is_table = bool(meta.get("is_table", False))
        if is_table or kind == "table":
            table_count += 1
        rows = meta.get("row_count", "—")
        cols = meta.get("col_count", "—")
        quality = meta.get("quality", "—")
        table_id = str(meta.get("table_id", "") or "")[:8]
        head = " ".join((payload.get("content") or "")[:110].split())
        print(f"    тип={kind} | is_table={is_table} | строк={rows} | колонок={cols} | "
              f"качество={quality} | table_id={table_id or '—'}")
        print(f"      начало: {head}")
    print(f"\n  ИТОГ: табличных фрагментов {table_count} из {len(points)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
