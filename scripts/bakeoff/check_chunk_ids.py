"""Сверка фрагментов Qdrant и Neo4j по идентификатору.

Зачем: владелец справедливо заметил, что распознанные фрагменты уже лежат в Qdrant и пересборка
не должна заново запускать распознавание. Это верно, если фрагмент из Qdrant находится в Neo4j
ПО ТОМУ ЖЕ идентификатору. Проверяем прямым поиском, а не сравнением случайных выборок (первая
версия этой проверки сравнивала две случайные выборки и дала ложный ноль).

Запуск в контейнере api:
  docker exec kag-api python /app/data/check_chunk_ids.py
Успех: все текстовые фрагменты (шаблон `<document_id>_chunk_NNNNN`) находятся в Neo4j по id.
Служебные фрагменты карточек («card») узлов Chunk не имеют — это норма и в счёт не идёт.
"""
import os
import re

from neo4j import GraphDatabase
from qdrant_client import QdrantClient

TEXT_CHUNK = re.compile(r"_chunk_\d+$")

qhost = os.environ.get("QDRANT_HOST", "qdrant").replace("https://", "").replace("http://", "")
client = QdrantClient(url=f"http://{qhost}:6333", api_key=os.environ.get("QDRANT_API_KEY") or None)
points, _ = client.scroll(collection_name="kag_documents", limit=15, with_payload=True, with_vectors=False)

uri = os.environ.get("NEO4J_URI") or f"bolt://{os.environ.get('NEO4J_HOST', 'neo4j')}:{os.environ.get('NEO4J_PORT', 7687)}"
driver = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"),
                                         os.environ.get("NEO4J_PASSWORD", "")))
rows = []
with driver.session() as s:
    for p in points:
        pl = p.payload or {}
        cid = str(pl.get("chunk_id") or "")
        doc = str(pl.get("document_id") or "")
        is_text = bool(TEXT_CHUNK.search(cid))
        found = s.run("MATCH (c:Chunk {id: $id}) RETURN count(c) AS n", id=cid).single()["n"] if cid else 0
        rows.append((cid, doc[:8], str(p.id)[:13], is_text, found))

print(f"{'chunk_id':<52} {'вид':<8} {'по id'}")
for cid, doc, pid, is_text, found in rows:
    print(f"{cid[:52]:<52} {'текст' if is_text else 'служебный':<8} {found}")

text_rows = [r for r in rows if r[3]]
ok = sum(1 for r in text_rows if r[4])
print(f"\nтекстовых фрагментов: {len(text_rows)}, найдено в Neo4j по идентификатору: {ok}")
print("ИТОГ: " + ("идентификаторы совпадают — пересборка переиспользует распознанные фрагменты "
                  "без повторного распознавания" if ok == len(text_rows) and text_rows else
                  "идентификаторы расходятся — нужна унификация ключа до пересборки"))
driver.close()
