"""Удаление файлов-сирот в /app/data/uploads (безопасная версия).

СИРОТА — файл, который НЕ принадлежит ни одному документу реестра. Определяется по
префиксу имени: файлы документов лежат как "{document_id}_{исходное имя}". Поэтому
сверка идёт по document_id, а НЕ по совпадению имени: иначе файлы живых документов
(у которых в БД имя без префикса) были бы сочтены сиротами и уничтожены.

Защита: документ считается защищённым, если на диске есть файл с префиксом его id,
либо файл с точно его именем. Удаляются только остальные.

Режим: DRY=1 — только показать; без DRY — удалить.
"""
import os
import re
import sys

from src.database.session import get_session_local
from src.database.document_models import Document

DRY = os.environ.get("DRY") == "1"
maker = get_session_local()
s = maker()
docs = [(r[0], r[1]) for r in s.query(Document.id, Document.filename).all()]
s.close()
ids = {str(i) for i, _ in docs}
names = {n for _, n in docs if n}

d = "/app/data/uploads"
files = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]
UUID_PREFIX = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_")

protected, orphans = set(), []
for f in files:
    m = UUID_PREFIX.match(f)
    if m and m.group(1) in ids:
        protected.add(m.group(1))
        continue
    if f in names:
        protected.add(f)
        continue
    orphans.append(f)

unprotected_docs = [i for i in ids if i not in protected]
total = sum(os.path.getsize(os.path.join(d, f)) for f in orphans)
print(f"документов в реестре: {len(ids)}")
print(f"файлов на диске: {len(files)}")
print(f"файлов живых документов: {len(files) - len(orphans)}")
print(f"СИРОТ: {len(orphans)}, объём {total/1024/1024:.1f} МБ")
print(f"документов без файла на диске: {len(unprotected_docs)}")

if DRY:
    print("\nDRY-режим: ничего не удалено. Примеры сирот:")
    for f in orphans[:10]:
        print("  ", f[:80])
    sys.exit(0)

if unprotected_docs:
    print("\nОСТАНОВ: есть документы без файла — сначала разобраться. Удаление не выполнено.")
    for i in unprotected_docs[:10]:
        print("  ", i)
    sys.exit(2)

removed, failed = 0, []
for f in orphans:
    try:
        os.remove(os.path.join(d, f))
        removed += 1
    except Exception as e:  # noqa: BLE001
        failed.append((f, str(e)))

print(f"\nудалено: {removed}, не удалось: {len(failed)}")
for f, e in failed[:10]:
    print("  ", f[:70], "|", e[:60])

rest = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]
print(f"осталось файлов: {len(rest)}")
