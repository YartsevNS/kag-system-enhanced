"""Найти фрагменты с испорченной кодировкой (UTF-8, прочитанный как Latin-1).

Признак: доля «типичных» символов двойной порчи (Ð, Ñ, Ã, Â) высокая, а нормальной кириллицы почти нет.
Так выглядит текст, который ушёл в распознавание как картинка и вернулся мусором (дефект из релиза
2026.10.10.13: 29 документов, 1959 фрагментов).

Печатает: сколько фрагментов и документов испорчено, примеры текста.
"""
import os
import re
from collections import defaultdict

from qdrant_client import QdrantClient

CYR = re.compile(r"[а-яА-ЯёЁ]")
BAD = re.compile(r"[ÐÑÃÂ�]")

q = os.environ.get("QDRANT_HOST", "qdrant").replace("https://", "").replace("http://", "")
c = QdrantClient(url=f"http://{q}:6333", api_key=os.environ.get("QDRANT_API_KEY") or None)

points, offset = [], None
while True:
    batch, offset = c.scroll(collection_name="kag_documents", limit=500, with_payload=True,
                             with_vectors=False, offset=offset)
    points.extend(batch)
    if offset is None or not batch:
        break

bad_docs = defaultdict(int)
total_docs = set()
examples = []
checked = bad = 0
for p in points:
    pl = p.payload or {}
    text = str(pl.get("content") or pl.get("text") or "")
    did = str(pl.get("document_id") or "")
    cid = str(pl.get("chunk_id") or "")
    if not text or "chunk" not in cid:
        continue
    checked += 1
    total_docs.add(did)
    cyr = len(CYR.findall(text))
    bad_chars = len(BAD.findall(text))
    # порог: символы порчи встречаются чаще, чем нормальная кириллица (в живом русском тексте порчи ~0)
    if bad_chars >= 5 and bad_chars > cyr * 2:
        bad += 1
        bad_docs[did] += 1
        if len(examples) < 3:
            examples.append((did, text[:150]))

print(f"фрагментов проверено: {checked}; с испорченной кодировкой: {bad}")
print(f"документов затронуто: {len(bad_docs)} из {len(total_docs)}")
print("\nпримеры:")
for did, ex in examples:
    print(f"   {did[:12]}: {ex}")
print("\nтоп документов по числу испорченных фрагментов:")
for did, n in sorted(bad_docs.items(), key=lambda x: -x[1])[:8]:
    print(f"   {did[:12]} — {n} фрагментов")
