"""Разбор файлов-сирот: что это за файлы, дубликаты ли они, годятся ли в обработку.

Зачем: в uploads/ 97 файлов без записи в реестре, и по именам не понять, мусор это или документы,
которые надо обработать. Прибор отвечает тремя фактами: расширения и размеры, СОДЕРЖИМОЕ мелких
текстовых (мусор от мониторинга или настоящий текст) и совпадение по исходному имени с документами
в базе (тогда это дубликат, а не пропущенный документ).

Запуск внутри контейнера api:  docker exec kag-api python /app/data/stand_orphans_detail.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter


def main() -> int:
    from sqlalchemy import text

    from src.database.session import get_session_local

    sess = get_session_local()()
    docs = sess.execute(text("select id, filename, chunks_count from documents")).fetchall()
    stored_ids = {r[0] for r in docs}
    names_in_db = {str(r[1] or "").lower() for r in docs}
    sess.close()

    uploads = "/app/data/uploads"
    orphans = []
    for name in sorted(os.listdir(uploads)):
        path = os.path.join(uploads, name)
        if not os.path.isfile(path) or name.split("_", 1)[0] in stored_ids:
            continue
        orphans.append((name, os.path.getsize(path)))

    print(f"файлов без записи в реестре: {len(orphans)}")
    ext = Counter(os.path.splitext(n)[1].lower() for n, _ in orphans)
    print(f"по расширениям: {dict(ext)}")
    big = [(n, s) for n, s in orphans if s >= 50_000]
    print(f"файлов от 50 КБ: {len(big)}; от 500 КБ: {len([1 for _, s in big if s >= 500_000])}")
    print(f"мелких (меньше 2 КБ): {len([1 for _, s in orphans if s < 2048])}")

    # Дубликаты: исходное имя (после <id>_) уже есть среди документов?
    dup = 0
    for name, _ in orphans:
        original = name.split("_", 1)[1].lower() if "_" in name else name.lower()
        if original in names_in_db:
            dup += 1
    print(f"совпадают по исходному имени с документами в базе (дубликаты): {dup}")

    print("\n--- СОДЕРЖИМОЕ МЕЛКИХ ТЕКСТОВЫХ (три самых мелких) ---")
    tiny = sorted([(n, s) for n, s in orphans if n.lower().endswith((".txt", ".md"))], key=lambda x: x[1])[:3]
    for name, size in tiny:
        print(f"  {name[:70]} ({size} б):")
        try:
            with open(os.path.join(uploads, name), encoding="utf-8", errors="replace") as f:
                content = f.read().replace("\n", " ")
            print(f"    {content[:240]}")
        except OSError as e:
            print(f"    не читается: {e}")

    print("\n--- КРУПНЫЕ ФАЙЛЫ (от 100 КБ): проверка, что это настоящие документы ---")
    for name, size in sorted([(n, s) for n, s in orphans if s >= 100_000], key=lambda x: -x[1])[:12]:
        path = os.path.join(uploads, name)
        head = b""
        try:
            with open(path, "rb") as f:
                head = f.read(5)
        except OSError:
            pass
        kind = "PDF" if head.startswith(b"%PDF") else f"не PDF ({head!r})"
        print(f"  {size/1024/1024:6.2f} МБ  {kind:<12} {name[:88]}")

    print("\n--- ТОП-5 ПО РАЗМЕРУ СРЕДИ ВСЕХ СИРОТ ---")
    for name, size in sorted(orphans, key=lambda x: -x[1])[:5]:
        print(f"  {size/1024/1024:6.2f} МБ  {name[:88]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
