"""Состояние системы: сводка метрик и живых проверок для страницы «Состояние системы».

Зачем отдельный эндпоинт, если есть /metrics: /metrics — это формат для Prometheus (тысячи
строк с служебными именами), человеку его читать нельзя. Здесь то же самое, но сведённое в
понятные блоки и посчитанное на месте: доли, средние, p50/p95 по стадиям, счётчики отказов
и оценок. Страница /system показывает это без Grafana и без доступа к внутренним портам.
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter
from loguru import logger

router = APIRouter(prefix="/api/v1/system", tags=["system"])

_STARTED_AT = time.time()


# ── Помощники: чтение значений из реестра prometheus_client ───────────────────
# Ничего не импортируем из сети и не ходим в Prometheus: считаем по собранным
# метрикам текущего процесса. Эндпоинт дешёвый и работает даже без мониторинга.

def _counter_sum(metric, label_filter: Optional[Dict[str, str]] = None) -> float:
    """Сумма значений счётчика (с фильтром по меткам)."""
    total = 0.0
    try:
        for fam in metric.collect():
            for s in fam.samples:
                if s.name.endswith("_created"):
                    continue
                if label_filter and any(s.labels.get(k) != v for k, v in label_filter.items()):
                    continue
                total += float(s.value or 0)
    except Exception:
        pass
    return total


def _counter_by(metric, label: str) -> Dict[str, float]:
    """Значения счётчика в разрезе одной метки: {"ok": 5, "error": 1}."""
    out: Dict[str, float] = {}
    try:
        for fam in metric.collect():
            for s in fam.samples:
                if s.name.endswith("_created"):
                    continue
                key = s.labels.get(label) or "unknown"
                out[key] = out.get(key, 0.0) + float(s.value or 0)
    except Exception:
        pass
    return out


def _hist_stats(metric, qs=(0.5, 0.95), label_filter: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Статистика гистограммы: количество, среднее и квантили (линейная интерполяция).

    Считаем сами, чтобы страница показывала p50/p95 без Prometheus — раньше это
    видел только тот, у кого есть Grafana и promql.
    """
    total = 0.0
    total_sum = 0.0
    buckets: Dict[float, float] = {}
    try:
        for fam in metric.collect():
            for s in fam.samples:
                if label_filter and any(s.labels.get(k) != v for k, v in label_filter.items()):
                    continue
                if s.name.endswith("_bucket"):
                    le = float(s.labels.get("le", "inf"))
                    buckets[le] = buckets.get(le, 0.0) + float(s.value or 0)
                elif s.name.endswith("_count"):
                    total += float(s.value or 0)
                elif s.name.endswith("_sum"):
                    total_sum += float(s.value or 0)
    except Exception:
        pass

    res: Dict[str, Any] = {
        "count": int(total),
        "avg": round(total_sum / total, 4) if total > 0 else None,
    }
    if total <= 0 or not buckets:
        return res

    for q in qs:
        target = q * total
        prev_le, prev_cum = 0.0, 0.0
        value = None
        for le in sorted(buckets):
            cum = buckets[le]
            if cum >= target:
                if le == float("inf"):
                    value = prev_le if prev_le else None
                else:
                    frac = (target - prev_cum) / (cum - prev_cum) if cum > prev_cum else 0.0
                    value = prev_le + (le - prev_le) * frac
                break
            prev_le, prev_cum = le, cum
        res[f"p{int(q * 100)}"] = round(value, 4) if value is not None else None
    return res


def _pct(part: float, whole: float) -> float:
    """Доля в процентах, безопасно при нуле."""
    return round(100.0 * part / whole, 1) if whole > 0 else 0.0


# ── Живые проверки (не метрики, а факты «сейчас») ─────────────────────────────

async def _live_checks() -> Dict[str, Any]:
    """Документы, векторы и очередь: то, что нельзя узнать из счётчиков процесса."""
    import asyncio

    async def _docs() -> Optional[int]:
        def _inner():
            from src.api.services.document_service import document_service
            return len(getattr(document_service, "_documents", []) or [])
        try:
            return await asyncio.wait_for(asyncio.to_thread(_inner), timeout=5)
        except Exception:
            return None

    async def _vectors() -> Optional[Dict[str, Any]]:
        def _inner():
            from src.api.services.qdrant_monitor import qdrant_monitor
            summary = qdrant_monitor.get_collections_summary() or []
            total = sum(int(c.get("points_count") or 0) for c in summary)
            return {
                "total": total,
                "collections": [
                    {"name": c.get("name") or c.get("collection"),
                     "points": int(c.get("points_count") or 0)}
                    for c in summary
                ],
            }
        try:
            return await asyncio.wait_for(asyncio.to_thread(_inner), timeout=6)
        except Exception:
            return None

    async def _queue() -> Optional[Dict[str, int]]:
        def _inner():
            from src.indexing.queue_guard import _redis
            r = _redis()
            if r is None:
                return None
            out = {}
            for q in ("celery", "maintenance"):
                try:
                    out[q] = int(r.llen(q) or 0)
                except Exception:
                    out[q] = 0
            return out
        try:
            return await asyncio.wait_for(asyncio.to_thread(_inner), timeout=5)
        except Exception:
            return None

    docs, vectors, queue = await asyncio.gather(_docs(), _vectors(), _queue())
    queue_total = sum((queue or {}).values()) if queue else None
    return {
        "documents": docs,
        "vectors": (vectors or {}).get("total") if vectors else None,
        "collections": (vectors or {}).get("collections") if vectors else None,
        "queue": queue or None,
        "queue_total": queue_total,
    }


# ── Сводка ────────────────────────────────────────────────────────────────────

@router.get("/overview", summary="Сводка состояния системы")
async def system_overview() -> Dict[str, Any]:
    """Сводка для страницы «Состояние системы»: запросы, стадии, ответы, отбор, токены."""
    from src.monitoring import prometheus as P

    http_total = _counter_sum(P.http_requests_total)
    by_status = _counter_by(P.http_requests_total, "status")
    errors = sum(v for k, v in by_status.items() if str(k).startswith(("4", "5")))

    stages = {}
    for stage in ("qdrant", "graph", "tables", "llm", "access"):
        st = _hist_stats(P.rag_stage_duration_seconds, label_filter={"stage": stage})
        if st.get("count"):
            stages[stage] = st

    answers = _counter_by(P.answers_total, "status")
    answers_n = sum(answers.values())

    rerun = _counter_sum(P.reranker_runs_total)
    rech = _counter_sum(P.reranker_changed_top1_total)

    live = await _live_checks()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "service": {
            "name": "kag-api",
            "healthy": True,
            "trace_id": True,
            "prometheus_scrape": "/metrics",
        },
        "requests": {
            "total": int(http_total),
            "errors": int(errors),
            "error_pct": _pct(errors, http_total),
            "by_status": {str(k): int(v) for k, v in sorted(by_status.items())},
            "duration": _hist_stats(P.http_request_duration_seconds, qs=(0.5, 0.95)),
        },
        "stages": stages,
        "answers": {
            "total": int(answers_n),
            "by_status": {str(k): int(v) for k, v in sorted(answers.items())},
            "ok_pct": _pct(answers.get("ok", 0.0), answers_n),
            "length_chars": _hist_stats(P.answer_length_chars, qs=(0.5,)),
        },
        "retrieval": {
            "fragments": _hist_stats(P.context_fragments, qs=(0.5,)),
            "cutoff_triggered": int(_counter_sum(P.cutoff_triggered_total)),
            "cutoff_dropped": _hist_stats(P.cutoff_dropped_fragments, qs=(0.5,)),
            "reranker_runs": int(rerun),
            "reranker_top1_changed": int(rech),
            "reranker_change_pct": _pct(rech, rerun),
        },
        "llm": {
            "calls_by_model": {k: int(v) for k, v in _counter_by(P.llm_requests_total, "model").items()},
            "tokens_prompt": int(_counter_sum(P.llm_tokens_total, {"type": "prompt"})),
            "tokens_completion": int(_counter_sum(P.llm_tokens_total, {"type": "completion"})),
            "duration_by_model": {},
        },
        "feedback": {
            "up": int(_counter_sum(P.feedback_total, {"value": "up"})),
            "down": int(_counter_sum(P.feedback_total, {"value": "down"})),
        },
        "corpus": live,
    }


@router.get("/health", summary="Короткая проверка живости с версией")
async def system_health() -> Dict[str, Any]:
    """Живость + аптайм: для плашек и внешнего мониторинга."""
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/feedback-stats", summary="Оценки ответов (для дашборда)")
async def feedback_stats() -> Dict[str, Any]:
    """Отдельно от сводки: страница чата показывает это рядом с ответом."""
    from src.monitoring import prometheus as P
    up = int(_counter_sum(P.feedback_total, {"value": "up"}))
    down = int(_counter_sum(P.feedback_total, {"value": "down"}))
    return {"up": up, "down": down, "total": up + down}


@router.post("/log-feedback", summary="Записать оценку ответа пользователем")
async def log_feedback(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Оценка ответа: метрика + строка в data/feedback.jsonl вместе с trace_id.

    trace_id — ключ связи: по нему в журнале видно весь путь запроса (поиск, граф,
    отсечение, модель), поэтому оценка «плохо» превращается в разбор, а не в догадку.
    """
    value = str(payload.get("value") or "").lower()
    if value not in ("up", "down"):
        return {"ok": False, "error": "value должен быть up или down"}

    trace_id = ""
    try:
        from src.api.trace import get_trace_id
        trace_id = get_trace_id() or ""
    except Exception:
        pass

    from src.monitoring.prometheus import record_feedback
    record_feedback(value)

    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "value": value,
        "trace_id": trace_id,
        "user": str(payload.get("user") or ""),
        "question": str(payload.get("question") or "")[:500],
        "answer_head": str(payload.get("answer") or "")[:300],
        "comment": str(payload.get("comment") or "")[:500],
    }
    try:
        import json
        import os
        path = os.environ.get("FEEDBACK_LOG", "/app/data/feedback.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[feedback] файл не записан: {e}")

    logger.info(f"FEEDBACK: {value} trace={trace_id} (всего up/down — см. /metrics)")
    return {"ok": True, "value": value, "trace_id": trace_id}
