"""Журнал ошибок онтологии: файл снаружи контейнера + таблица в Postgres.

Зачем два места:
- файл (/var/log/kag/ontology/errors.log, bind-каталог с хоста) читается, когда
  контейнер лежит или упал: tail/jq на сервере, без api и без БД;
- таблица ontology_errors — то, что показывает страница /errors: фильтры,
  отметка «решено», история.

Сюда попадает ТОЛЬКО онтология: применение схемы, версии схемы, извлечение и запись
фактов в TypeDB, нарушения ключей/кардинальности/enum. Ошибки HTTP, авторизации и
прочих подсистем идут в общий журнал приложения.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_BYTES = 20 * 1024 * 1024

# Типы держим закрытым списком: свободный текст в поле типа превращает фильтр
# на странице в бесполезный.
ERROR_TYPES = (
    "insert_failed",
    "validation_failed",
    "schema_drift",
    "extraction_failed",
    "enum_violation",
    "cardinality_violation",
    "key_mismatch",
)
SOURCES = ("parser", "extraction", "validator", "writer", "typedb")

_LOCK = threading.Lock()


def _file() -> Path:
    """Файл журнала: /var/log/kag/ontology, если каталог смонтирован с хоста.

    Пока монтирования нет (контейнер создан без тома), пишем в bind-каталог данных
    /app/data — он тоже виден на хосте (/home/yartsevn/kag-system/data). Оба пути
    переживают пересоздание контейнера; при появлении тома журнал сам переедет
    в /var/log/kag/ontology без правки кода.
    """
    env = os.getenv("ONTOLOGY_ERRORS_FILE", "").strip()
    if env:
        return Path(env)
    mounted = Path("/var/log/kag/ontology")
    if mounted.is_dir() and os.access(mounted, os.W_OK):
        return mounted / "errors.log"
    return Path("/app/data/ontology-errors/errors.log")


def _session():
    """Сессия БД тем же путём, что остальной api (KAG_DB_URL/пароли из настроек)."""
    from src.database.session import get_session_local

    return get_session_local()()


_DDL = """
CREATE TABLE IF NOT EXISTS ontology_errors (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ts            timestamptz NOT NULL DEFAULT now(),
  level         text NOT NULL DEFAULT 'error',
  error_type    text NOT NULL,
  document_key  text,
  document_id   text,
  entity_id     text,
  entity_type   text,
  message       text NOT NULL,
  details       jsonb NOT NULL DEFAULT '{}'::jsonb,
  source        text NOT NULL DEFAULT 'extraction',
  resolved      boolean NOT NULL DEFAULT false,
  resolved_at   timestamptz,
  resolved_by   text
)
"""


def ensure_table() -> None:
    from sqlalchemy import text

    db = _session()
    try:
        conn = db
        conn.execute(text(_DDL))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ontology_errors_ts_idx ON ontology_errors (ts DESC)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ontology_errors_doc_idx ON ontology_errors (document_key)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ontology_errors_type_idx ON ontology_errors (error_type)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ontology_errors_open_idx "
            "ON ontology_errors (resolved) WHERE NOT resolved"
        ))
        db.commit()
    finally:
        db.close()


def log_error(
    message: str,
    *,
    error_type: str = "insert_failed",
    source: str = "writer",
    level: str = "error",
    document_key: str = "",
    document_id: str = "",
    entity_id: str = "",
    entity_type: str = "",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Дописать ошибку в файл и в таблицу. Никогда не бросает наружу."""
    et = error_type if error_type in ERROR_TYPES else "insert_failed"
    src = source if source in SOURCES else "writer"
    # Время в файле — московское: файл читают с хоста глазами, а не машиной.
    # В БД ts остаётся timestamptz (UTC внутри, Postgres отдаёт по сессии) — страница
    # показывает его в Europe/Moscow.
    msk = timezone(timedelta(hours=3))
    record = {
        "ts": datetime.now(msk).isoformat(timespec="seconds"),
        "level": "warning" if level in ("warn", "warning") else "error",
        "error_type": et,
        "document_key": document_key or None,
        "document_id": document_id or None,
        "entity_id": entity_id or None,
        "entity_type": entity_type or None,
        "message": (message or "")[:2000],
        "details": details or {},
        "source": src,
        "resolved": False,
    }

    # 1) Файл — первым: он нужен, когда БД недоступна.
    try:
        f = _file()
        f.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _LOCK:
            if f.exists() and f.stat().st_size > _MAX_BYTES:
                try:
                    f.replace(f.with_suffix(".log.1"))
                except Exception:  # noqa: BLE001
                    pass
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("журнал онтологии: файл не записан: %s", str(exc)[:160])

    # 2) Таблица — для страницы.
    try:
        from sqlalchemy import text

        ensure_table()
        db = _session()
        try:
            db.execute(
                text(
                    "INSERT INTO ontology_errors "
                    "(level, error_type, document_key, document_id, entity_id, entity_type, "
                    " message, details, source) "
                    "VALUES (:level, :error_type, :document_key, :document_id, :entity_id, "
                    ":entity_type, :message, CAST(:details AS jsonb), :source)"
                ),
                {
                    "level": record["level"], "error_type": et,
                    "document_key": record["document_key"], "document_id": record["document_id"],
                    "entity_id": record["entity_id"], "entity_type": record["entity_type"],
                    "message": record["message"],
                    "details": json.dumps(record["details"], ensure_ascii=False),
                    "source": src,
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("журнал онтологии: строка в БД не записана: %s", str(exc)[:160])


def list_errors(
    limit: int = 200,
    offset: int = 0,
    level: str = "",
    error_type: str = "",
    document_key: str = "",
    resolved: Optional[bool] = None,
    src: str = "",
) -> Dict[str, Any]:
    """Страница читает таблицу; файл — запасной путь, если БД недоступна."""
    from sqlalchemy import text

    where, params = [], {}
    if level:
        where.append("level = :level")
        params["level"] = level
    if error_type:
        where.append("error_type = :error_type")
        params["error_type"] = error_type
    if document_key:
        where.append("coalesce(document_key,'') ILIKE :dk")
        params["dk"] = f"%{document_key}%"
    if resolved is not None:
        where.append("resolved = :resolved")
        params["resolved"] = resolved
    if src:
        where.append("source = :src")
        params["src"] = src
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit or 200), 2000))
    offset = max(0, int(offset or 0))

    try:
        ensure_table()
        db = _session()
        try:
            total = db.execute(text(f"SELECT count(*) FROM ontology_errors{cond}"), params).scalar()
            rows = db.execute(
                text(
                    "SELECT id::text, ts, level, error_type, document_key, document_id, entity_id, "
                    "entity_type, message, details, source, resolved, resolved_at, resolved_by "
                    f"FROM ontology_errors{cond} ORDER BY ts DESC LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": limit, "offset": offset},
            ).mappings().all()
        finally:
            db.close()
        items = []
        for r in rows:
            item = dict(r)
            item["ts"] = item["ts"].isoformat() if item.get("ts") else None
            item["resolved_at"] = item["resolved_at"].isoformat() if item.get("resolved_at") else None
            items.append(item)
        return {"total": int(total or 0), "items": items, "source": "db", "file": str(_file())}
    except Exception as exc:  # noqa: BLE001 — БД недоступна: отдаём файл
        logger.warning("журнал онтологии: чтение из БД не удалось: %s", str(exc)[:160])
        return {"total": 0, "items": _read_file(limit), "source": "file",
                "file": str(_file()), "db_error": str(exc)[:200]}


def _read_file(limit: int) -> List[Dict[str, Any]]:
    f = _file()
    if not f.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        for raw in reversed(f.read_text(encoding="utf-8", errors="replace").splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except Exception:  # noqa: BLE001
                item = {"ts": "", "level": "error", "error_type": "insert_failed",
                        "message": raw[:2000], "details": {}, "source": "writer",
                        "resolved": False}
            item["id"] = ""
            out.append(item)
            if len(out) >= limit:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("журнал онтологии: файл не прочитан: %s", str(exc)[:160])
    return out


def get_error(error_id: str) -> Optional[Dict[str, Any]]:
    from sqlalchemy import text

    ensure_table()
    db = _session()
    try:
        r = db.execute(
            text(
                "SELECT id::text, ts, level, error_type, document_key, document_id, entity_id, "
                "entity_type, message, details, source, resolved, resolved_at, resolved_by "
                "FROM ontology_errors WHERE id = CAST(:id AS uuid)"
            ),
            {"id": error_id},
        ).mappings().first()
    finally:
        db.close()
    if not r:
        return None
    item = dict(r)
    item["ts"] = item["ts"].isoformat() if item.get("ts") else None
    item["resolved_at"] = item["resolved_at"].isoformat() if item.get("resolved_at") else None
    return item


def resolve_error(error_id: str, resolved_by: str) -> bool:
    from sqlalchemy import text

    ensure_table()
    db = _session()
    try:
        res = db.execute(
            text(
                "UPDATE ontology_errors SET resolved = true, resolved_at = now(), "
                "resolved_by = :by WHERE id = CAST(:id AS uuid) AND NOT resolved"
            ),
            {"id": error_id, "by": (resolved_by or "")[:120]},
        )
        db.commit()
        return bool(res.rowcount)
    finally:
        db.close()


def journal_path() -> str:
    return str(_file())
