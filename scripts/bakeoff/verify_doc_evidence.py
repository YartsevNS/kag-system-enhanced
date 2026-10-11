"""Проверка цитат у связей документа ПРЯМО В ГРАФЕ — замер конвейера, а не щупа.

Что считает: у каждой смысловой связи, записанной из фрагментов документа, лежит поле evidence
(дословная цитата). Проверяем, что эта цитата реально найдена в тексте СВОЕГО фрагмента (текст
хранится в узле Chunk). Это и есть главная метрика приёмки: «доля связей с подтверждённой цитатой».

Зачем отдельный прибор: связь между сущностями в графе не висит на фрагменте — привязка лежит в
свойстве связи chunk_id, поэтому «доля по документу» считается не глазами, а запросом:
    связи с chunk_id из фрагментов документа → их evidence → сравнение с текстом того же фрагмента.
Нормализация — та же, что при извлечении: сравниваем только буквы и цифры.

    docker exec kag-api python /tmp/verify_doc_evidence.py <document_id> [показать_примеров]
"""

import re
import sys
from collections import Counter

DOC = sys.argv[1] if len(sys.argv) > 1 else "2873e687-d476-43e6-b94e-f4dd14eaead8"
SHOW = int(sys.argv[2]) if len(sys.argv) > 2 else 5

from src.indexing.knowledge_graph import kg_service  # noqa: E402


def flat(x: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", (x or "").lower()).strip()


with kg_service.driver.session() as s:
    chunks = {str(r["id"]): (r["text"] or "") for r in s.run(
        "MATCH (d:Document {id: $doc})-[:HAS_CHUNK]->(c:Chunk) RETURN c.id AS id, c.text AS text",
        doc=DOC)}
    if not chunks:
        print("у документа нет узлов фрагментов в графе — граф не построен")
        raise SystemExit(2)
    rels = [dict(r) for r in s.run(
        """MATCH (a)-[r]->(b)
           WHERE r.chunk_id IN $ids AND type(r) <> 'MENTIONS' AND type(r) <> 'HAS_CHUNK'
           RETURN type(r) AS t, coalesce(r.evidence, '') AS ev, r.chunk_id AS cid""",
        ids=list(chunks))]

print("=" * 90)
print(f"ПРОВЕРКА ЦИТАТ В ГРАФЕ | документ {DOC}")
print(f"фрагментов в графе: {len(chunks)} | смысловых связей: {len(rels)}")
print("=" * 90)

total = len(rels)
with_ev = 0
in_text = 0
no_ev_type: Counter = Counter()
by_type: dict[str, dict[str, int]] = {}
examples = []
for r in rels:
    cid = str(r.get("cid") or "")
    text = flat(chunks.get(cid, ""))
    ev = flat(str(r.get("ev") or ""))
    t = str(r.get("t") or "")
    c = by_type.setdefault(t, {"всего": 0, "с_цитатой": 0, "в_тексте": 0})
    c["всего"] += 1
    if not ev:
        no_ev_type[t] += 1
        continue
    with_ev += 1
    c["с_цитатой"] += 1
    if ev[:60] in text or ev in text:
        in_text += 1
        c["в_тексте"] += 1
    elif len(examples) < SHOW:
        examples.append((t, str(r.get("ev"))[:70], str(chunks.get(cid, ""))[:80]))

print(f"связей всего:                     {total}")
print(f"  с непустой цитатой:             {with_ev}")
print(f"  ЦИТАТА НАЙДЕНА В ТЕКСТЕ:        {in_text}"
      f"   -> ТОЧНОСТЬ ПОДТВЕРЖДЕНИЯ {int(100 * in_text / total) if total else 0} %")
print(f"  связей без цитаты:              {total - with_ev}"
      f"   -> доля без подтверждения {int(100 * (total - with_ev) / total) if total else 0} %")
if examples:
    print("\nпримеры, где цитата НЕ найдена в своём фрагменте:")
    for t, ev, tx in examples:
        print(f"  [{t}] цитата: {ev!r}")
        print(f"        начало фрагмента: {tx!r}")
print("\nпо типам связей (всего / с цитатой / цитата в тексте):")
for t, c in sorted(by_type.items(), key=lambda kv: -kv[1]["всего"]):
    print(f"  {t:24s} {c['всего']:5d} / {c['с_цитатой']:5d} / {c['в_тексте']:5d}")
if no_ev_type:
    print("\nбез цитаты по типам:")
    for t, n in no_ev_type.most_common(10):
        print(f"  {t:24s} {n:5d}")
