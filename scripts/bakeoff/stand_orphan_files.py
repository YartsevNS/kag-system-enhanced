"""Прибор: файлы в uploads БЕЗ записи в базе — то, что лежит необработанным.

Зачем: в очереди пусто и все документы в базе «completed», но это не значит, что весь корпус обработан.
Файлы, которые залили и не поставили в обработку (или залили в обход API), видны только сравнением
каталога с реестром. Прибор печатает их списком с размерами и датами, чтобы можно было решить, что
с ними делать, и не «угадывать» объём работы.

Запуск внутри контейнера api:  docker exec kag-api python /app/data/stand_orphan_files.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter


def main() -> int:
    from sqlalchemy import text

    from src.database.session import get_session_local

    sess = get_session_local()()
    rows = sess.execute(text("select id, filename from documents")).fetchall()
    stored = {r[0]: (r[1] or "") for r in rows}
    sess.close()

    uploads = "/app/data/uploads"
    if not os.path.isdir(uploads):
        print(f"нет каталога {uploads}")
        return 1

    orphans = []
    for name in sorted(os.listdir(uploads)):
        path = os.path.join(uploads, name)
        if not os.path.isfile(path):
            continue
        # Имя файла в uploads: <document_id>_<исходное имя>
        doc_id = name.split("_", 1)[0]
        if doc_id in stored:
            continue
        st = os.stat(path)
        orphans.append((name, st.st_size, st.st_mtime))

    print(f"файлов в uploads: {len(os.listdir(uploads))}; документов в базе: {len(stored)}; "
          f"файлов без записи: {len(orphans)}")
    if not orphans:
        return 0

    ext = Counter(os.path.splitext(n)[1].lower() for n, _, _ in orphans)
    print(f"по расширениям: {dict(ext)}")
    total_mb = sum(s for _, s, _ in orphans) / 1024 / 1024
    print(f"суммарный размер: {total_mb:.1f} МБ")

    print("\nсамые крупные 15:")
    for name, size, mtime in sorted(orphans, key=lambda x: -x[1])[:15]:
        import datetime
        when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {size/1024/1024:8.2f} МБ  {when}  {name[:96]}")

    print("\nпервые 25 по имени:")
    for name, size, _ in orphans[:25]:
        print(f"  {size/1024:8.1f} КБ  {name[:96]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
