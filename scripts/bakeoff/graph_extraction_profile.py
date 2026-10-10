"""Профиль извлечения графа: чем строим, сколько вызовов на фрагмент, сколько это занимает.

Зачем прибор. Разговор «ускорить граф» без числа превращается в догадки. Здесь снимается ФАКТИЧЕСКИЙ
профиль нашей схемы: настройка функции graph (провайдер, модель, режим, размер промпта), сколько записей
в кэше извлечения и под какой версией, и замер одного реального извлечения на фрагменте (с обходом кэша
через уникальный хвост текста). По этим трём числам видно, где теряется время: в модели, в числе вызовов
на фрагмент или в промахах кэша.

Стоимость: один прогон = столько вызовов LLM, сколько делает режим (у нас 2). Меньше рубля.

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/graph_extraction_profile.py [--chunks 1]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from sqlalchemy import text


def count_cache(sess) -> dict:
    out = {}
    rows = sess.execute(text(
        "select count(*) from system_configs where id like 'entity_cache:%'")).scalar()
    out["всего записей кэша"] = rows
    out["версий кэша"] = sess.execute(text(
        "select count(distinct split_part(id, ':', 2)) from system_configs "
        "where id like 'entity_cache:%'")).scalar()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=1, help="сколько фрагментов замерить")
    args = ap.parse_args()

    from src.api.services.config_store import config_store
    from src.database.session import get_session_local
    from src.indexing.entity_extractor import entity_extractor

    sess = get_session_local()()
    print("=== ЧЕМ СТРОИМ ГРАФ (настройка функции graph) ===")
    cfg = entity_extractor._get_graph_config() or {}
    prompt = ""
    try:
        prompt = (config_store.get("function_map", "graph") or {}).get("system_prompt", "") or ""
    except Exception:  # noqa: BLE001
        pass
    print(f"  провайдер: {cfg.get('provider')} | модель: {cfg.get('model')} | адрес: {cfg.get('url')}")
    print(f"  ключ задан: {bool(cfg.get('api_key'))} | температура: {(cfg.get('parameters') or {}).get('temperature')}")
    print(f"  режим извлечения: {cfg.get('extraction_mode', 'two_pass')} (two_pass = 2 вызова на фрагмент)")
    print(f"  размер системного промпта: {len(prompt)} символов")
    mode = cfg.get("extraction_mode", "two_pass")
    calls_per_chunk = 1 if str(mode).startswith("single") else 2

    print("\n=== КЭШ ИЗВЛЕЧЕНИЯ ===")
    for k, v in count_cache(sess).items():
        print(f"  {k}: {v}")
    print(f"  версия кэша сейчас: {entity_extractor.cache_version(cfg)[:24]}")

    print("\n=== ОБЪЁМ РАБОТЫ ===")
    chunks = sess.execute(text("select count(*) from documents where chunks_count > 0")).scalar()
    total_chunks = sess.execute(text("select coalesce(sum(chunks_count),0) from documents")).scalar()
    print(f"  документов с фрагментами: {chunks}; фрагментов всего: {total_chunks}")
    print(f"  вызовов LLM на весь корпус при текущем режиме: ~{int(total_chunks) * calls_per_chunk}")

    print(f"\n=== ЗАМЕР ОДНОГО ИЗВЛЕЧЕНИЯ ({args.chunks} фрагмент(ов)) ===")
    rows = sess.execute(text(
        "select id, chunks_count from documents where chunks_count > 1 "
        "order by created_at desc limit :n"), {"n": args.chunks}).fetchall()
    sess.close()
    if not rows:
        print("  не нашёл документов с фрагментами")
        return 1

    from src.config import get_settings

    s = get_settings()
    qbase = f"http://{s.QDRANT_HOST}:{s.QDRANT_PORT}"
    qheaders = {"Content-Type": "application/json"}
    if getattr(s, "QDRANT_API_KEY", ""):
        qheaders["api-key"] = s.QDRANT_API_KEY

    import json as _json
    import urllib.request as _url

    for doc_id, _ in rows:
        # Текст фрагмента берём напрямую из Qdrant (как это делает конвейер), а не через внутренние
        # поля сервиса: в maintenance-процессе клиент сервиса не инициализирован, и обращение к нему
        # даёт AttributeError вместо замера.
        samples = []
        for coll in ("kag_documents", "kag_news"):
            body = _json.dumps({"filter": {"must": [{"key": "document_id", "match": {"value": doc_id}}]},
                                "limit": 3, "with_payload": True}).encode()
            req = _url.Request(f"{qbase}/collections/{coll}/points/scroll", body, qheaders, method="POST")
            try:
                with _url.urlopen(req, timeout=30) as r:
                    pts = _json.loads(r.read().decode())["result"]["points"]
            except Exception:  # noqa: BLE001
                pts = []
            samples += [(p.get("payload") or {}).get("content", "") for p in pts]
        samples = [t for t in samples if t and len(t) > 50]
        if not samples:
            print(f"  у документа {doc_id[:8]} не нашлось текста фрагментов в Qdrant")
            continue
        text_sample = samples[0] + "  " + os.urandom(4).hex()   # хвост, чтобы кэш не подменил замер
        t0 = time.time()
        res = asyncio.run(entity_extractor.extract_from_chunk(
            text_sample, chunk_id="profile", document_id=doc_id, filename="profile"))
        took = time.time() - t0
        w = res.get("warnings") or []
        print(f"  фрагмент {len(text_sample)} символов: {took:.1f} с | "
              f"сущностей {len(res.get('entities') or [])} | связей {len(res.get('relations') or [])} | "
              f"предупреждения {w}")
        print(f"    на один вызов LLM: ~{took / max(1, calls_per_chunk):.1f} с (режим {mode})")
        print(f"    пересчёт на корпус: {int(total_chunks) * calls_per_chunk * (took / max(1, calls_per_chunk)) / 3600:.0f} часов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
