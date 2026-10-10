"""Выгрузка текстов документов для разметки (запуск В КОНТЕЙНЕРЕ api).

Зачем отдельный шаг: ключ JEV лежит в окружении рабочей машины, а не контейнера, поэтому текст
едет наружу, а ответы — обратно. Здесь только СЫРЬЁ (то, что модель увидит), без секретов.

Кладём в /app/data/label_docs_input.json: [{document_id, title, text}], где text — короткая шапка
документа: название, сводка, темы и первые фрагменты (модель размечает ДОКУМЕНТ, а не фрагмент:
пилот показал, что вид и тема на уровне фрагмента определяются плохо).

Запуск: docker exec kag-api python /app/data/export_docs_for_labeling.py
"""
from __future__ import annotations

import asyncio
import json
import os

OUT = os.environ.get("OUT", "/app/data/label_docs_input.json")
MAX_CHARS = int(os.environ.get("MAX_CHARS", "2600"))


async def main() -> int:
    from src.api.services.document_repository import get_doc_repo
    from src.indexing.embeddings_service import service_for_document

    docs = get_doc_repo().get_all()
    items = []
    for did, d in docs.items():
        head = []
        title = str(d.get("recognized_title") or d.get("filename") or "")
        if title:
            head.append(f"Название: {title}")
        if d.get("summary"):
            head.append(f"О чём: {d['summary']}")
        topics = d.get("topics") or []
        if isinstance(topics, list) and topics:
            head.append("Ключевые слова: " + ", ".join(str(t) for t in topics[:8]))
        try:
            chunks = await service_for_document(did).get_document_chunks(did)
        except Exception:  # noqa: BLE001 — без фрагментов размечаем по шапке
            chunks = []
        body = ""
        for ch in chunks[:2]:
            meta = ch.get("metadata") or ch
            body += str(meta.get("text") or meta.get("content") or "") + "\n"
        text = ("\n".join(head) + "\n\n" + body)[:MAX_CHARS]
        if not text.strip():
            continue
        items.append({"document_id": did, "title": title, "text": text})

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    print(f"документов выгружено: {len(items)} → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
