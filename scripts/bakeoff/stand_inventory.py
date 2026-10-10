"""Инвентаризация стенда одним прибором: что не обработано, что с OCR, откуда берём документы.

Зачем прибор, а не россыпь команд по ssh: половина ответов лежит в БД и в настройках (config_store),
а сборка таких запросов строкой через ssh ломается на кавычках — в отчёт попадает «пусто» вместо цифры.
Прибор печатает ЧИСЛА и списки, по которым видно, есть ли работа и чем она ограничена.

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/stand_inventory.py
"""
from __future__ import annotations

import json
import os
import sys


def _hr(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    from sqlalchemy import text

    from src.database.session import get_session_local

    sess = get_session_local()()

    _hr("ДОКУМЕНТЫ ПО СТАТУСАМ")
    for row in sess.execute(text(
            "select status, count(*), min(created_at)::date, max(created_at)::date "
            "from documents group by 1 order by 2 desc")):
        print(f"  {row[0]:<12} {row[1]:>5}   с {row[2]} по {row[3]}")

    _hr("ГОТОВНОСТЬ КОНВЕЙЕРА: ЧТО РЕАЛЬНО ЕСТЬ У ДОКУМЕНТОВ")
    row = sess.execute(text(
        "select count(*), "
        "count(*) filter (where chunks_count = 0), "
        "count(*) filter (where coalesce(recognized_title,'') = ''), "
        "count(*) filter (where rubrics is null or rubrics = '[]'), "
        "count(*) filter (where facets is null or facets = '{}'), "
        "count(*) filter (where label_provenance is null or label_provenance = '{}'), "
        "count(*) filter (where coalesce(collection,'') = 'news') "
        "from documents")).first()
    labels = ["всего", "без фрагментов", "без названия", "без темы", "без фасетов",
              "без провенанса", "новости (другая коллекция)"]
    for name, value in zip(labels, row):
        print(f"  {name:<34} {value}")

    _hr("КОЛЛЕКЦИИ ВЕКТОРОВ (Qdrant)")
    try:
        import urllib.request

        from src.config import get_settings

        s = get_settings()
        base = f"http://{s.QDRANT_HOST}:{s.QDRANT_PORT}"
        headers = {"api-key": s.QDRANT_API_KEY} if getattr(s, "QDRANT_API_KEY", "") else {}
        for name in (s.QDRANT_COLLECTION, s.QDRANT_NEWS_COLLECTION):
            try:
                req = urllib.request.Request(f"{base}/collections/{name}", headers=headers)
                with urllib.request.urlopen(req, timeout=15) as r:
                    info = json.loads(r.read().decode())["result"]
                print(f"  {name:<16} точек {info.get('points_count')} "
                      f"статус {info.get('status')} indexed {info.get('indexed_vectors_count')}")
            except Exception as e:  # noqa: BLE001
                print(f"  {name:<16} ошибка: {type(e).__name__} {str(e)[:80]}")
    except Exception as e:  # noqa: BLE001
        print(f"  настройки недоступны: {e}")

    _hr("ГРАФ (Neo4j)")
    try:
        from src.indexing.knowledge_graph import kg_service

        def cypher(q: str):
            res = kg_service.execute_cypher(q)
            # Контракт ответа приборам не гарантирован: может прийти список строк, словарь с данными
            # или строка ошибки — разбираем все три, иначе проверка молча печатает объект.
            if isinstance(res, dict):
                rows = res.get("data") or res.get("rows") or res.get("records") or []
                return rows if rows else res
            return res

        for label in ("Document", "Chunk", "Entity"):
            print(f"  узлов {label:<10} {cypher(f'MATCH (n:{label}) RETURN count(n) AS c')}")
        print(f"  связей всего      {cypher('MATCH ()-[r]->() RETURN count(r) AS c')}")
        print(f"  фрагментов с версией схемы "
              f"{cypher('MATCH (n:Chunk) WHERE n.schema_version IS NOT NULL RETURN count(n) AS c')}")
        print(f"  связей с версией извлечения "
              f"{cypher('MATCH ()-[r]->() WHERE r.extractor_version IS NOT NULL RETURN count(r) AS c')}")
    except Exception as e:  # noqa: BLE001
        print(f"  ошибка доступа к графу: {type(e).__name__} {str(e)[:140]}")

    _hr("ОЧЕРЕДИ (Redis)")
    try:
        import redis as _redis

        from src.config import get_settings as _gs

        s = _gs()
        r = _redis.Redis(host=s.REDIS_HOST, port=s.REDIS_PORT, db=1, socket_timeout=10)
        for q in ("documents", "maintenance", "celery"):
            print(f"  {q:<14} {r.llen(q)}")
    except Exception as e:  # noqa: BLE001
        print(f"  redis недоступен: {type(e).__name__} {str(e)[:80]}")

    _hr("НАСТРОЙКИ, ОТ КОТОРЫХ ЗАВИСИТ ОБРАБОТКА")
    for cid, value in sess.execute(text(
            "select id, value from system_configs "
            "where id like 'ocr:%' or id like 'system:%' or id like 'search:%' "
            "or id like 'chunk%' or id like 'graph:%' order by id")):
        text_value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        print(f"  {cid:<34} {text_value[:160]}")

    _hr("ИСТОЧНИКИ СКАЧИВАНИЯ (веб-монитор)")
    try:
        rows = sess.execute(text(
            "select url, last_checked, coalesce(last_hash,'') from watched_urls "
            "order by last_checked desc nulls last limit 40")).fetchall()
        print(f"  записей: {len(rows)}")
        for url, checked, last_hash in rows:
            print(f"  {str(checked)[:19]:<20} хеш {last_hash[:12]:<13} {str(url)[:100]}")
    except Exception as e:  # noqa: BLE001
        print(f"  ошибка: {type(e).__name__} {str(e)[:140]}")

    _hr("ОБСЛУЖИВАНИЕ САЙТОВ: СЛЕДЫ СКАЧИВАНИЙ (журнал обработки)")
    try:
        from src.indexing import document_kinds  # noqa: F401 — проверка импортируемости слоёв конвейера
        rows = sess.execute(text(
            "select type, count(*) from notifications group by 1 order by 2 desc")).fetchall()
        for t, c in rows:
            print(f"  уведомлений {t:<20} {c}")
    except Exception as e:  # noqa: BLE001
        print(f"  ошибка: {type(e).__name__} {str(e)[:140]}")

    _hr("ФАЙЛЫ БЕЗ ЗАПИСЕЙ И ЗАПИСИ БЕЗ ФАЙЛОВ")
    try:
        uploads = "/app/data/uploads"
        files = {f for f in os.listdir(uploads)} if os.path.isdir(uploads) else set()
        ids = {r[0] for r in sess.execute(text("select id from documents"))}
        print(f"  файлов в uploads: {len(files)}; документов в базе: {len(ids)}")
    except Exception as e:  # noqa: BLE001
        print(f"  ошибка: {type(e).__name__} {str(e)[:120]}")

    sess.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
