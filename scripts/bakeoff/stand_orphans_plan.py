"""План обработки файлов-сирот: что действительно новый документ, а что дубликат.

Зачем по ХЕШУ, а не по имени: один и тот же файл, залитый дважды, получает разные id, а исходное имя
может совпасть у РАЗНЫХ документов (в корпусе такие есть). Сравнение SHA-256 с полем `file_hash` в
реестре даёт однозначный ответ: «этот файл уже в базе» или «этот документ не обрабатывали».

На выходе: сводка (сколько новых/дубликатов/служебных) и файл-план со списком новых — по нему дальше
идёт заливка и обработка, а не «на глаз».

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/stand_orphans_plan.py [--write-plan /app/data/orphans_plan.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

# Служебные «карточки» мониторинга: заголовок + ссылка на источник, несколько сотен байт.
# Они не документы: обрабатывать их нечего, но и терять нельзя — это след источника (см. отчёт).
CARD_MAX_BYTES = 2048
DOC_EXTS = {".pdf", ".docx", ".doc", ".rtf", ".odt", ".md", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-plan", default="")
    ap.add_argument("--uploads", default="/app/data/uploads")
    ap.add_argument("--dirs", default="",
                    help="через запятую: каталоги с СКАЧАННЫМИ корпусами — посчитать, сколько из них новых")
    args = ap.parse_args()

    from sqlalchemy import text

    from src.database.session import get_session_local

    sess = get_session_local()()
    rows = sess.execute(text("select id, filename, file_hash, chunks_count, status from documents")).fetchall()
    sess.close()
    stored_ids = {r[0] for r in rows}
    hashes = {str(r[2] or "") for r in rows if r[2]}

    new_docs, duplicates, cards, other = [], [], [], []
    for name in sorted(os.listdir(args.uploads)):
        path = os.path.join(args.uploads, name)
        if not os.path.isfile(path) or name.split("_", 1)[0] in stored_ids:
            continue
        size = os.path.getsize(path)
        ext = os.path.splitext(name)[1].lower()
        if size < CARD_MAX_BYTES and ext in (".txt", ".md"):
            cards.append((name, size))
            continue
        if ext not in DOC_EXTS:
            other.append((name, size, ext))
            continue
        digest = sha256(path)
        if digest in hashes:
            duplicates.append((name, size, digest))
        else:
            new_docs.append({"filename": name, "size": size, "sha256": digest,
                             "path": path, "ext": ext})

    print(f"всего сирот: {len(new_docs) + len(duplicates) + len(cards) + len(other)}")
    print(f"  НОВЫЕ документы (хеша нет в базе): {len(new_docs)}")
    print(f"  дубликаты (хеш уже в базе):        {len(duplicates)}")
    print(f"  служебные карточки (<{CARD_MAX_BYTES} б, txt): {len(cards)}")
    print(f"  прочее:                            {len(other)} {[o[2] for o in other]}")
    print(f"\nобъём новых: {sum(d['size'] for d in new_docs)/1024/1024:.1f} МБ "
          f"({Counter(d['ext'] for d in new_docs)})")
    print("\nновые по размеру:")
    for d in sorted(new_docs, key=lambda x: -x["size"]):
        print(f"  {d['size']/1024/1024:6.2f} МБ  {d['filename'][:92]}")

    if args.write_plan:
        with open(args.write_plan, "w", encoding="utf-8") as f:
            json.dump({"new": new_docs, "duplicates": [d[0] for d in duplicates],
                       "cards": [c[0] for c in cards], "other": [o[0] for o in other]},
                      f, ensure_ascii=False, indent=1)
        print(f"\nплан записан: {args.write_plan}")

    if args.dirs:
        print("\n=== СКАЧАННЫЕ КОРПУСА: СКОЛЬКО ИЗ НИХ НОВЫЕ ===")
        for folder in [d.strip() for d in args.dirs.split(",") if d.strip()]:
            if not os.path.isdir(folder):
                print(f"  {folder}: нет каталога")
                continue
            fresh, known, skipped = 0, 0, 0
            volume = 0
            for root, _dirs, files in os.walk(folder):
                for fname in files:
                    path = os.path.join(root, fname)
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in DOC_EXTS:
                        skipped += 1
                        continue
                    size = os.path.getsize(path)
                    if size < CARD_MAX_BYTES:
                        skipped += 1
                        continue
                    digest = sha256(path)
                    if digest in hashes:
                        known += 1
                    else:
                        fresh += 1
                        volume += size
            print(f"  {folder}: новых {fresh}, уже в базе {known}, пропущено {skipped}, "
                  f"объём новых {volume/1024/1024:.1f} МБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
