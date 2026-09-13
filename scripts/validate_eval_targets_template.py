"""Проверка набора: каждый эталонный документ существует и имеет чанки в Qdrant.

Зачем: вопрос, чей эталон пуст (документ удалён, переиндексируется или не имеет чанков),
не может быть найден никогда — это не промах поиска, а сломанная разметка. Живой случай:
у документа «Информационное письмо о профиле защиты ПО» в момент проверки было 0 чанков
(шла переиндексация), и вопрос по нему был недостижим.

Запуск (в контейнере): docker exec -i kag-api python - < этот_файл
"""
import json

from src.api.services.document_repository import get_doc_repo

TARGETS = __TARGETS__  # noqa: F821  (подставляется генератором)

# сколько чанков видно в БД (быстрая проверка; точное число точек — в Qdrant ниже)
docs = get_doc_repo().get_all() or {}
missing = []
empty = []
ok = 0
for idx, q, ids in TARGETS:
    for did in ids:
        d = docs.get(did)
        if not isinstance(d, dict):
            missing.append((idx, did))
            continue
        if int(d.get("chunks_count") or 0) <= 0:
            empty.append((idx, did, str(d.get("filename"))[:40], str(d.get("status"))))
        else:
            ok += 1

print("  эталонных документов проверено:", sum(len(i[2]) for i in TARGETS))
print("  в порядке:", ok)
print("  НЕ найдены в БД:", missing if missing else "нет")
print("  MISSING_INDICES=" + json.dumps(sorted({i[0] for i in missing})))
print("  без чанков (вопрос недостижим):")
for idx, did, fname, status in empty:
    print(f"     вопрос #{idx}: {did[:8]} | {fname} | статус {status}")
if not empty:
    print("     нет")
print("  EMPTY_INDICES=" + json.dumps(sorted({i[0] for i in empty})))
