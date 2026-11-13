"""Конвейер обработки: этапы, их длительность и объём документа.

Зачем: сейчас длительность этапов лежит в логах процесса (config_store, ключ
`process_logs:log_<document_id>`), но человеку её негде увидеть — приходится идти
в /logs и читать сырые записи. Нужен один экран: документ, его размер и число
фрагментов, и таблица «этап → сколько занял → доля от общего времени».

Сбор можно выключить (настройка `system/process_logs.collect`): на больших прогонах
запись каждого шага в config_store — лишняя работа, а иногда она не нужна вовсе.
Выключение влияет только на запись, консольный лог остаётся.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

CATEGORY = "process_logs"
FLAG_CATEGORY = "system"
FLAG_KEY = "process_logs"

# Человеческие названия шагов: в логе это машинные коды, на экране — то, что понятно.
STEP_TITLES = {
    "start": "старт",
    "find_file": "поиск файла",
    "parse": "разбор документа",
    "parser": "разбор документа",
    "ocr": "распознавание (OCR)",
    "render": "рендер страниц",
    "tables": "таблицы",
    "chunk": "нарезка на фрагменты",
    "chunking": "нарезка на фрагменты",
    "analyze": "анализ документа",
    "document_analysis": "анализ документа",
    "vectorize": "векторизация",
    "embed": "векторизация",
    "graph": "граф (Neo4j)",
    "graph_triage": "триаж графа",
    "graph_skipped": "граф пропущен",
    "typedb": "онтология TypeDB",
    "typedb_write": "онтология TypeDB",
    "types": "определение вида и темы",
    "doc_context": "контекст документа",
    "sections": "разделы",
    "completed": "готово",
    "error": "ошибка",
}


def enabled() -> bool:
    """Собирать ли статистику этапов. По умолчанию — да."""
    try:
        from src.api.services.config_store import config_store

        cfg = config_store.get(FLAG_CATEGORY, FLAG_KEY) or {}
        if isinstance(cfg, dict) and "collect" in cfg:
            return bool(cfg["collect"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("конвейер: флаг сбора не прочитан: %s", str(exc)[:120])
    return True


def set_enabled(value: bool, who: str = "") -> bool:
    from src.api.services.config_store import config_store

    ok = config_store.set(FLAG_CATEGORY, FLAG_KEY, {"collect": bool(value), "changed_by": who})
    logger.info("конвейер: сбор статистики %s (%s)", "включён" if value else "выключен", who or "—")
    return bool(ok)


def _logs() -> Dict[str, List[Dict[str, Any]]]:
    from src.api.services.config_store import config_store

    try:
        return config_store.get_all(CATEGORY) or {}
    except Exception:  # noqa: BLE001
        return {}


def _doc_row(document_id: str) -> Dict[str, Any]:
    from sqlalchemy import text

    from src.database.session import get_session_local

    db = get_session_local()()
    try:
        row = db.execute(text(
            "SELECT filename, file_size, chunks_count, status, collection, created_at "
            "FROM documents WHERE id = :id"), {"id": document_id}).mappings().first()
    except Exception:  # noqa: BLE001
        row = None
    finally:
        db.close()
    if not row:
        return {}
    return {
        "filename": row["filename"] or "",
        "size_bytes": int(row["file_size"] or 0),
        "chunks": int(row["chunks_count"] or 0),
        "status": row["status"] or "",
        "collection": row["collection"] or "",
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
    }


def documents(limit: int = 30) -> List[Dict[str, Any]]:
    """Документы, у которых есть лог этапов: свежие первыми."""
    logs = _logs()
    items = []
    for key, entries in logs.items():
        if not key.startswith("log_") or not entries:
            continue
        doc_id = key[4:]
        first, last = entries[0], entries[-1]
        info = _doc_row(doc_id)
        items.append({
            "document_id": doc_id,
            "filename": info.get("filename", ""),
            "size_bytes": info.get("size_bytes", 0),
            "chunks": info.get("chunks", 0),
            "status": info.get("status", ""),
            "started_at": first.get("timestamp"),
            "finished_at": last.get("timestamp"),
            "total_ms": float(last.get("elapsed_ms") or 0),
            "steps": len(entries),
        })
    items.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    return items[:max(1, min(int(limit), 200))]


def detail(document_id: str) -> Dict[str, Any]:
    """Этапы одного документа: время каждого, доля от общего, ошибки."""
    logs = _logs()
    entries = logs.get(f"log_{document_id}") or []
    info = _doc_row(document_id)
    out: Dict[str, Any] = {
        "document_id": document_id,
        **info,
        "collecting": enabled(),
        "stages": [],
        "errors": [],
    }
    if not entries:
        out["note"] = "лога этапов для этого документа нет"
        return out

    # В лог одного документа дописываются ВСЕ прогоны (save() расширяет список).
    # Показываем последний прогон: иначе «доля этапа» считается против общего времени
    # другого прогона и получается 155% (живой случай 13.11.2026).
    starts = [i for i, e in enumerate(entries) if str(e.get("step")) in ("start", "find_file")]
    if starts:
        entries = entries[starts[-1]:]
    total = float(entries[-1].get("elapsed_ms") or 0)
    out["total_ms"] = total
    out["started_at"] = entries[0].get("timestamp")
    out["finished_at"] = entries[-1].get("timestamp")

    stages = []
    for e in entries:
        step = str(e.get("step") or "")
        delta = float(e.get("delta_ms") or 0)
        details = e.get("details") or {}
        stages.append({
            "step": step,
            "title": STEP_TITLES.get(step, step),
            "delta_ms": delta,
            "elapsed_ms": float(e.get("elapsed_ms") or 0),
            "share": round(delta / total * 100, 1) if total else None,
            "detail": _short(details),
        })
        if isinstance(details, dict) and details.get("error"):
            out["errors"].append({"step": step, "error": str(details["error"])[:300]})
    out["stages"] = stages

    # Самая дорогая стадия — то, ради чего экран и нужен.
    if stages:
        worst = max(stages, key=lambda s: s["delta_ms"])
        out["slowest"] = {"step": worst["step"], "title": worst["title"],
                          "ms": worst["delta_ms"], "share": worst["share"]}
    return out


def _short(details: Any) -> str:
    """Короткая строка «что произошло на шаге» — без простыни JSON."""
    if not isinstance(details, dict):
        return str(details)[:160]
    keys = ("ok", "empty", "error", "chunks", "vectors_stored", "entities", "relations",
            "pages", "tables", "reason", "engine", "elapsed")
    parts = [f"{k}={details[k]}" for k in keys if k in details and details[k] not in (None, "")]
    return ", ".join(parts)[:200] or str(details)[:160]


def snapshot(limit: int = 30) -> Dict[str, Any]:
    """Список документов + детали последнего, у которого есть лог."""
    docs = documents(limit)
    latest = detail(docs[0]["document_id"]) if docs else None
    return {"collecting": enabled(), "documents": docs, "latest": latest}
