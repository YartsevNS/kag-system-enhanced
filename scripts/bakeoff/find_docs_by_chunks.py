"""Найти документы с нужным числом фрагментов — для замеров (например, «документ на 20 чанков»).

    docker exec kag-api python /tmp/find_docs_by_chunks.py [от] [до] [сколько]

Печатает документ, число фрагментов, вид и начало имени. Ничего не меняет.
"""

import sys

LO = int(sys.argv[1]) if len(sys.argv) > 1 else 18
HI = int(sys.argv[2]) if len(sys.argv) > 2 else 26
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 12

from sqlalchemy import text  # noqa: E402

from src.database.session import get_db  # noqa: E402

db = next(get_db())
rows = db.execute(text(
    "SELECT d.id, d.filename, d.chunks_count, "
    "COALESCE(d.document_type, ''), COALESCE(d.collection, '') "
    "FROM documents d WHERE d.chunks_count BETWEEN :lo AND :hi AND d.status = 'completed' "
    "ORDER BY d.chunks_count LIMIT :lim"), {"lo": LO, "hi": HI, "lim": LIMIT}).fetchall()
print(f"документы с {LO}-{HI} фрагментами: {len(rows)}")
for r in rows:
    kind = f" | {r[3]}" if r[3] else ""
    coll = f" | {r[4]}" if r[4] else ""
    print(f"  {r[0]} | фрагментов {r[2]:3d} | {str(r[1])[:55]}{kind}{coll}")
