"""Проверка новой схемы v2 на живых фрагментах (модуль src/indexing/extraction_v2.py).

Быстрая проверка на 3 фрагментах. ОСНОВНОЙ прибор — scripts/bakeoff/audit_v2.py: у него уровни
чанк / набор / документ, проверка пар по онтологии, счёт времени и токенов и сохранение СЫРЫХ
ответов модели в JSON; запуск без выката — run_audit_v2.sh. Этот файл оставлен как разовый щуп.

Отправляет фрагменты через extract_chunk_v2 — то есть ровно тем запросом, который станет рабочим:
фрагмент целиком, образец ответа с цитатой, мягкие правила, терпимый разбор, проверка цитаты.
Печатает по каждому фрагменту и итог: сколько связей, у скольких подтверждена цитата.

    docker exec kag-api python /tmp/probe_v2.py <документ> [сколько_фрагментов]
"""

import asyncio
import re
import sys

DOC = sys.argv[1] if len(sys.argv) > 1 else "2873e687-d476-43e6-b94e-f4dd14eaead8"
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 3

from src.indexing.embeddings_service import service_for_document  # noqa: E402
from src.indexing.extraction_v2 import extract_chunk_v2  # noqa: E402

chunks = asyncio.run(service_for_document(DOC).get_document_chunks(DOC))
step = max(1, len(chunks) // COUNT)
picked = [chunks[i] for i in range(0, len(chunks), step)][:COUNT]
print("фрагментов в документе:", len(chunks), "| к проверке:", len(picked))

REL = EV = FOUND = 0
for ch in picked:
    cid = str(ch.get("chunk_id") or "")
    text = str(ch.get("content") or ch.get("text") or "")
    res = extract_chunk_v2(text, cid, require_evidence=False)
    rels = res.get("relations") or []

    def flat(x):
        return re.sub(r"[^0-9a-zа-яё]+", " ", (x or "").lower()).strip()

    t = flat(text)
    ev = sum(1 for r in rels if str(r.get("evidence") or "").strip())
    found = sum(1 for r in rels
                if flat(str(r.get("evidence") or "")) and flat(str(r.get("evidence")))[:60] in t)
    REL += len(rels)
    EV += ev
    FOUND += found
    print(f"  {cid[-8:]}: знаков {len(text)} | связей {len(rels)} | с цитатой {ev} | цитата в тексте {found}")
    for r in rels[:2]:
        print("      ", r.get("type"), "|", str(r.get("source"))[:26], "->", str(r.get("target"))[:26],
              "|", str(r.get("evidence"))[:45])

print("=== ИТОГ ПО СХЕМЕ v2 ===")
print("  связей:", REL, "| с цитатой:", EV, "| ЦИТАТА НАЙДЕНА В ТЕКСТЕ:", FOUND,
      "| подтверждено:", (int(100 * FOUND / REL) if REL else 0), "%")
