#!/usr/bin/env python3
"""Сверка Qdrant/Neo4j с таблицей documents: найти и (опционально) удалить сирот.

Зачем: записи в БД и векторы в Qdrant живут в разных хранилищах. Если том
postgres пересоздали (или документ удалили в обход API), чанки в Qdrant и узлы
в Neo4j остаются — поиск продолжает отдавать документы, которых нет в списке
документов: их нельзя удалить/reindex из интерфейса, а ответы чата цитируют
«призраков». Скрипт находит рассинхрон и умеет его убрать.

Безопасность: по умолчанию только показывает (dry-run). Удаление — с --apply.
Файлы в data/uploads по умолчанию НЕ удаляются (векторы всегда можно
пересоздать из файлов, поэтому удаление обратимо).

Использование (на сервере 18):
    set -a; . ./.env; set +a       # секреты из env, не аргументами

    # 1. Что рассинхронизировано (ничего не меняет)
    python3 scripts/cleanup_orphans.py

    # 2. Удалить сирот из Qdrant и Neo4j (файлы остаются)
    python3 scripts/cleanup_orphans.py --apply

    # 3. Удалить и файлы (унести в ./data/orphans_removed/, не rm)
    python3 scripts/cleanup_orphans.py --apply --move-files

Скрипт только stdlib. Neo4j — через `docker exec <контейнер> cypher-shell`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional


def _q(url: str, api_key: str, payload: Optional[dict] = None, timeout: float = 120.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("api-key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def qdrant_documents(base: str, api_key: str, collection: str) -> Dict[str, int]:
    """{document_id: число точек} по всей коллекции."""
    counts: Dict[str, int] = {}
    offset = None
    while True:
        payload: dict = {"limit": 1000, "with_payload": ["document_id"], "with_vector": False}
        if offset is not None:
            payload["offset"] = offset
        res = _q(f"{base}/collections/{collection}/points/scroll", api_key, payload)["result"]
        for p in res["points"]:
            did = (p.get("payload") or {}).get("document_id")
            counts[did] = counts.get(did, 0) + 1
        offset = res.get("next_page_offset")
        if offset is None:
            return counts


def qdrant_count(base: str, api_key: str, collection: str, document_id: str) -> int:
    res = _q(f"{base}/collections/{collection}/points/count", api_key,
             {"exact": True, "filter": {"must": [{"key": "document_id", "match": {"value": document_id}}]}})
    return int((res.get("result") or {}).get("count") or 0)


def qdrant_delete(base: str, api_key: str, collection: str, document_id: str) -> bool:
    filt = {"must": [{"key": "document_id", "match": {"value": document_id}}]}
    try:
        _q(f"{base}/collections/{collection}/points/delete", api_key, {"filter": filt})
    except urllib.error.HTTPError as e:
        print(f"      Qdrant delete: HTTP {e.code} {e.read().decode()[:120]}")
        return False
    left = qdrant_count(base, api_key, collection, document_id)
    if left:
        print(f"      Qdrant: после удаления осталось {left} точек — НЕ удалено")
        return False
    return True


def neo4j_query(container: str, password: str, cypher: str) -> str:
    r = subprocess.run(["docker", "exec", container, "cypher-shell", "-u", "neo4j",
                        "-p", password, "--format", "plain", cypher],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        return "ERR: " + r.stderr.strip()[:200]
    return r.stdout.strip()


def neo4j_delete(container: str, password: str, document_id: str) -> bool:
    out = neo4j_query(container, password,
                      f'MATCH (d:Document {{id: "{document_id}"}}) '
                      f'OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c) DETACH DELETE c, d RETURN count(*)')
    if out.startswith("ERR"):
        print(f"      Neo4j: {out}")
        return False
    return True


def neo4j_count_orphan_chunks(container: str, password: str) -> int:
    """Chunk-узлы, не привязанные ни к одному Document (мусор после удаления)."""
    out = neo4j_query(container, password,
                      "MATCH (c:Chunk) WHERE NOT exists((:Document)-[:HAS_CHUNK]->(c)) "
                      "RETURN count(c)")
    if out.startswith("ERR"):
        return -1
    try:
        return int(out.splitlines()[-1].strip().strip('"'))
    except (ValueError, IndexError):
        return -1


def neo4j_delete_orphan_chunks(container: str, password: str) -> bool:
    """Удалить Chunk-узлы без родительского Document.

    DETACH DELETE документа снимает связь HAS_CHUNK, но сам чанк остаётся, если
    у него не было другого документа (а ещё остаются чанки старых прогонов,
    когда узел Document уже удалён). Такие узлы в графе бесполезны: без
    документа их не найти ни одним запросом, но они занимают место и попадают
    в обходы графа.
    """
    out = neo4j_query(container, password,
                      "MATCH (c:Chunk) WHERE NOT exists((:Document)-[:HAS_CHUNK]->(c)) "
                      "DETACH DELETE c RETURN count(*)")
    if out.startswith("ERR"):
        print(f"      Neo4j (мусорные чанки): {out}")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Сверка/очистка сирот Qdrant и Neo4j")
    ap.add_argument("--qdrant-url", default="http://localhost:6333")
    ap.add_argument("--qdrant-key", default=os.environ.get("QDRANT_API_KEY", ""),
                    help="по умолчанию $QDRANT_API_KEY (не передавать аргументом — видно в ps)")
    ap.add_argument("--collection", default="kag_documents")
    ap.add_argument("--db-container", default="kag-kag-db")
    ap.add_argument("--db-user", default="kag")
    ap.add_argument("--db-name", default="kag")
    ap.add_argument("--db-password", default=os.environ.get("KAG_DB_PASSWORD", ""))
    ap.add_argument("--neo4j-container", default="kag-neo4j")
    ap.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    ap.add_argument("--uploads-dir", default="/home/yartsevn/kag-system/data/uploads")
    ap.add_argument("--only", default="", help="regex: обрабатывать только эти document_id")
    ap.add_argument("--exclude", default="", help="regex: не трогать эти document_id")
    ap.add_argument("--apply", action="store_true", help="реально удалять (иначе только показать)")
    ap.add_argument("--skip-neo4j", action="store_true")
    ap.add_argument("--move-files", action="store_true",
                    help="перенести файлы сирот в ./data/orphans_removed/ (не удалять)")
    ap.add_argument("--report", default="/tmp/orphans_cleanup.json")
    args = ap.parse_args()

    def psql(sql: str) -> List[str]:
        r = subprocess.run(["docker", "exec", "-i", "-e", f"PGPASSWORD={args.db_password}",
                            args.db_container, "psql", "-U", args.db_user, "-d", args.db_name,
                            "-tAF|", "-c", sql], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print("psql ошибка:", r.stderr.strip()[:200])
            return []
        return [l for l in r.stdout.strip().split("\n") if l]

    db_ids = {l.split("|")[0] for l in psql("SELECT id FROM documents")}
    counts = qdrant_documents(args.qdrant_url, args.qdrant_key, args.collection)
    orphans = sorted(set(counts) - db_ids)
    if args.only:
        rx = re.compile(args.only)
        orphans = [d for d in orphans if rx.search(d)]
    if args.exclude:
        rx = re.compile(args.exclude)
        orphans = [d for d in orphans if not rx.search(d)]

    neo_ids: List[str] = []
    orphan_chunks = -1
    if not args.skip_neo4j:
        raw = neo4j_query(args.neo4j_container, args.neo4j_password, "MATCH (d:Document) RETURN d.id")
        if not raw.startswith("ERR"):
            neo_ids = [l.strip().strip('"') for l in raw.splitlines()[1:] if l.strip()]
        orphan_chunks = neo4j_count_orphan_chunks(args.neo4j_container, args.neo4j_password)
    neo_orphans = [d for d in neo_ids if d not in db_ids]

    print(f"БД documents:        {len(db_ids)}")
    print(f"Qdrant document_id:  {len(counts)} (точек {sum(counts.values())})")
    if not args.skip_neo4j:
        print(f"Neo4j Document:      {len(neo_ids)} (вне БД {len(neo_orphans)})")
        print(f"Neo4j Chunk без документа: {orphan_chunks if orphan_chunks >= 0 else 'н/д'}")
    print(f"СИРОТЫ (Qdrant − БД): {len(orphans)} документов, "
          f"{sum(counts[d] for d in orphans)} точек")
    print(f"режим: {'УДАЛЕНИЕ (--apply)' if args.apply else 'dry-run (ничего не меняется)'}\n")

    ups = {p.name.split("_", 1)[0]: p for p in Path(args.uploads_dir).iterdir() if p.is_file()} \
        if Path(args.uploads_dir).is_dir() else {}
    moved_dir = Path(args.uploads_dir).parent / "orphans_removed"

    report = {"db_documents": len(db_ids), "qdrant_documents": len(counts),
              "neo4j_documents": len(neo_ids), "orphan_documents": len(orphans),
              "orphan_points": sum(counts[d] for d in orphans), "applied": bool(args.apply),
              "items": []}
    deleted_q, deleted_n, moved = 0, 0, 0
    for d in orphans:
        pts = counts[d]
        f = ups.get(d)
        item = {"document_id": d, "points": pts, "file": str(f) if f else None,
                "qdrant_deleted": False, "neo4j_deleted": False, "file_moved": None}
        print(f"  {pts:>4} точек  {d}  {f.name[:70] if f else '(файла нет)'}")
        if args.apply:
            item["qdrant_deleted"] = qdrant_delete(args.qdrant_url, args.qdrant_key, args.collection, d)
            deleted_q += int(item["qdrant_deleted"])
            if not args.skip_neo4j and d in neo_orphans:
                item["neo4j_deleted"] = neo4j_delete(args.neo4j_container, args.neo4j_password, d)
                deleted_n += int(item["neo4j_deleted"])
            if args.move_files and f:
                moved_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(moved_dir / f.name))
                item["file_moved"] = str(moved_dir / f.name)
                moved += 1
        report["items"].append(item)

    report["deleted_qdrant"] = deleted_q
    report["deleted_neo4j"] = deleted_n
    report["files_moved"] = moved
    report["orphan_chunks_before"] = orphan_chunks
    if args.apply and not args.skip_neo4j:
        # Хвост в графе: чанки без родительского документа (снимаются только
        # связью HAS_CHUNK, сам узел остаётся при DETACH DELETE документа).
        report["orphan_chunks_cleaned"] = neo4j_delete_orphan_chunks(
            args.neo4j_container, args.neo4j_password)
        print(f"\nNeo4j: чанки без документа было {orphan_chunks}, "
              f"удаление -> {report['orphan_chunks_cleaned']}")
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.apply:
        left = qdrant_documents(args.qdrant_url, args.qdrant_key, args.collection)
        report["after"] = {"qdrant_documents": len(left), "points": sum(left.values())}
        print(f"\nОсталось в Qdrant: {len(left)} document_id, {sum(left.values())} точек")
        print(f"Удалено: Qdrant {deleted_q}, Neo4j {deleted_n}, файлов перенесено {moved}")
    else:
        print("\nЭто dry-run. Для удаления повторите с --apply")
    print(f"Отчёт: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
