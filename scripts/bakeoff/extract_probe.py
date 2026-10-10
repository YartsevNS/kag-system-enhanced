"""Прямой зонд извлечения: что модель реально возвращает по связям (сырые типы).

Нужен потому, что граф собрался без мусора, но и без смысловых типов: все связи упали
в RELATED_TO. Значит словарь типов не покрывает формулировки модели. Пока не увидим сырой
ответ, гадать бессмысленно — этот прибор печатает типы ровно так, как их написала модель.

Запуск на стенде:
    docker exec kag-api python /app/data/extract_probe.py
    docker exec kag-api python /app/data/extract_probe.py --filename "Указание Банка России от 15.06.2026 № 7369-У.txt"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, "/app")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filename", default="", help="какой документ взять (по умолчанию — любой небольшой)")
    ap.add_argument("--chars", type=int, default=1500)
    args = ap.parse_args()

    from qdrant_client.http import models as qm

    from src.api.services.document_repository import get_doc_repo
    from src.indexing.embeddings_service import embeddings_service
    from src.indexing.entity_extractor import entity_extractor
    from src.indexing.graph_ontology import normalize_relation, prompt_rules, validate_triple

    svc = embeddings_service
    if callable(svc) and not hasattr(svc, "initialize"):
        svc = svc()
    if not svc.is_initialized():
        await svc.initialize()

    target_id, target_name = None, ""
    for did, d in (get_doc_repo().get_all() or {}).items():
        if not isinstance(d, dict):
            continue
        if args.filename and (d.get("filename") or "") != args.filename:
            continue
        if int(d.get("chunks_count") or 0) > 0:
            target_id, target_name = did, d.get("filename") or ""
            break
    if not target_id:
        print("документ не найден")
        return 2

    points, _ = svc._qdrant_client.scroll(
        collection_name=svc.collection_name, limit=3,
        scroll_filter=qm.Filter(must=[qm.FieldCondition(
            key="document_id", match=qm.MatchValue(value=target_id))]),
        with_payload=True, with_vectors=False)
    if not points:
        print("у документа нет фрагментов")
        return 2
    payload = points[0].payload or {}
    text = (payload.get("content") or payload.get("text") or "")[: args.chars]
    print(f"документ: {target_name}")
    print(f"фрагмент: {text[:150]!r}…\n")

    result = await entity_extractor.extract_from_chunk(
        chunk_text=text, chunk_id="probe", document_id=target_id, filename=target_name)

    entities = result.get("entities") or []
    relations = result.get("relations") or []
    print(f"сущностей: {len(entities)}")
    for e in entities[:10]:
        print(f"   {str(e.get('type')):14s} {str(e.get('name'))[:70]}")
    print(f"\nсвязей: {len(relations)}")
    types_map = {str(e.get("name") or "").strip().lower(): str(e.get("type") or "").lower()
                 for e in entities}
    for r in relations:
        raw = str(r.get("type"))
        code = normalize_relation(raw)
        src_t = types_map.get(str(r.get("source") or "").strip().lower(), "")
        dst_t = types_map.get(str(r.get("target") or "").strip().lower(), "")
        ok, note = validate_triple(src_t, code, dst_t)
        print(f"   {raw[:34]:36s} → канон {code:14s} {'ПИШЕМ' if ok else 'ОТКАЗ'} "
              f"{src_t} → {dst_t} {note}")
    print(f"\nпредупреждения модели: {result.get('warnings')}")
    print(f"\nдлина правил онтологии в задании: {len(prompt_rules())} знаков")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
