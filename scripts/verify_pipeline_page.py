"""Проверка страницы «Конвейер обработки» на реальных логах стенда.

Вызывает настоящий сервис (не HTTP): список документов с логом, детали последнего,
и состояние выключателя сбора. Печатает этапы с временем — чтобы было видно,
что экран показывает не пустоту.
"""
import json

from src.api.services import pipeline

snap = pipeline.snapshot(limit=10)
out = {
    "collecting": snap["collecting"],
    "documents": [
        {"id": d["document_id"][:8], "file": d["filename"][:40], "chunks": d["chunks"],
         "size_mb": round(d["size_bytes"] / 1024 / 1024, 1), "total_s": round(d["total_ms"] / 1000, 1),
         "steps": d["steps"]}
        for d in snap["documents"]
    ],
}
latest = snap.get("latest") or {}
out["latest"] = {
    "file": (latest.get("filename") or "")[:50],
    "chunks": latest.get("chunks"),
    "size_mb": round((latest.get("size_bytes") or 0) / 1024 / 1024, 1),
    "total_s": round((latest.get("total_ms") or 0) / 1000, 1),
    "slowest": latest.get("slowest"),
    "stages": [
        {"step": s["step"], "s": round(s["delta_ms"] / 1000, 1), "share": s["share"]}
        for s in (latest.get("stages") or [])
    ][:14],
    "errors": latest.get("errors"),
}
print(json.dumps(out, ensure_ascii=False, indent=1))
