"""Приёмка на РЕАЛЬНЫХ файлах: взять необработанные файлы из папки импорта и прогнать весь конвейер.

Зачем: все проверки до этого шли на уже загруженных документах. Настоящая приёмка — взять файлы, которые
в базе ещё НЕ обрабатывались, и посмотреть, что получится на выходе: текст, векторы, граф, поиск.

Что делает:
  1. обходит папку импорта, считает хеши и отбрасывает то, что уже есть в базе (иначе это не проверка,
     а повторная обработка);
  2. берёт N самых «полезных» новых файлов (сначала те, что не крошечные карточки);
  3. заливает их штатным путём (document_service.upload_document) и ставит в очередь обработки.

Запуск на стенде:
    docker exec kag-api python /app/data/accept_real_files.py --limit 5 --dry-run
    docker exec kag-api python /app/data/accept_real_files.py --limit 5 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import pathlib
import sys

sys.path.insert(0, "/app")

ALLOWED = {".pdf", ".txt", ".md", ".docx", ".csv", ".png", ".jpg", ".jpeg"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/app/data/inbox")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-size", type=int, default=3000, help="отсеки служебные карточки")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    from src.api.services.document_repository import get_doc_repo

    repo = get_doc_repo()
    known = set()
    for rec in (repo.get_all() or {}).values():
        h = rec.get("file_hash") if isinstance(rec, dict) else None
        if h:
            known.add(h)

    root = pathlib.Path(args.root)
    candidates = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in ALLOWED:
            continue
        size = p.stat().st_size
        if size < args.min_size:
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest in known:
            continue
        candidates.append({"path": p, "size": size, "hash": digest})

    # Разные типы важнее одинаковых: берём по одному на расширение, потом остальные.
    picked, seen_ext = [], set()
    for c in sorted(candidates, key=lambda x: -x["size"]):
        if c["path"].suffix.lower() not in seen_ext:
            picked.append(c)
            seen_ext.add(c["path"].suffix.lower())
    for c in sorted(candidates, key=lambda x: -x["size"]):
        if c not in picked:
            picked.append(c)
    picked = picked[: args.limit]

    print("=" * 92)
    print(f"ПРИЁМКА НА РЕАЛЬНЫХ ФАЙЛАХ — {'ЗАЛИВКА И ОБРАБОТКА' if apply else 'ОТБОР'}")
    print("=" * 92)
    print(f"в папке импорта новых (нетронутых) файлов: {len(candidates)}; берём: {len(picked)}")
    for c in picked:
        print(f"  {c['path'].name[:56]:58s} {round(c['size'] / 1024)} КБ  {c['path'].suffix.lower()}")
    if not picked:
        print("новых файлов нет — проверять нечего (всё уже в базе)")
        return 0
    if not apply:
        print("\nэто отбор — ничего не залито. Для заливки добавить --apply")
        return 0

    from src.api.services.document_service import document_service
    from src.indexing.queue_guard import enqueue_document

    print("\n=== ЗАЛИВКА ===")
    for c in picked:
        try:
            rec = await document_service.upload_document(
                filename=c["path"].name, file_content=c["path"].read_bytes(),
                file_type=c["path"].suffix.lower(), uploaded_by=None,
                source_metadata={"acceptance": "real-files"},
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ОШИБКА {c['path'].name[:40]}: {type(exc).__name__}: {exc}")
            continue
        queued = False
        try:
            queued = bool(enqueue_document(rec.document_id, force=True))
        except Exception as exc:  # noqa: BLE001
            print(f"  (в очередь не поставлен: {exc})")
        print(f"  {c['path'].name[:46]:48s} документ {rec.document_id[:8]} | в очередь: {queued}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
