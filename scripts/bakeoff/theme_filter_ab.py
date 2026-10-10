"""Замер «до/после» перехода фильтра темы с легаси-домена на рубрики — на ОДНОМ образе.

Зачем так. Сравнивать два состояния по очереди нельзя: корпус и payload между прогонами меняются,
и разницу не отличить от дрейфа данных. Поэтому берём ОДИН поиск без фильтра (top-50) и применяем оба
фильтра в памяти — фильтр только ВЫБРАСЫВАЕТ фрагменты, поэтому top-10 после фильтрации равен тому,
что сервер отдал бы с серверным фильтром (при достаточном top-50).

Что мерим по каждому вопросу (у набора есть relevant_document_ids):
  * legacy: выдача с фильтром по прежнему полю `domain` (мягкий режим: пустой домен допускается);
  * rubric:  выдача с фильтром по полю тем `rubrics` (мягкий режим: без темы допускается);
  * без фильтра — контрольная точка.
Метрики: hit@1, hit@3, MRR по документам-эталонам и число вопросов, где эталон вообще не найден.

Значение домена берём ТАМ ЖЕ, где его берёт чат — у классификатора вопроса
(`chat_service._detect_query_analysis`): иначе замер мерил бы не то, что делает продукт.

Запуск в контейнере api:
  docker exec kag-api python /app/data/theme_filter_ab.py --questions /app/data/eval_questions_v3.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


def payload_of(chunk: dict) -> dict:
    """Все поля результата поиска: сам ответ Qdrant + вложенные metadata (там лежит копия)."""
    meta = chunk.get("metadata") or {}
    return {**meta, **chunk}


def doc_id_of(chunk: dict) -> str:
    """document_id лежит на ВЕРХНЕМ уровне результата (в payload он тоже есть, но не в metadata)."""
    return str(payload_of(chunk).get("document_id") or "")


def rubrics_of(chunk: dict) -> list:
    raw = payload_of(chunk).get("rubrics")
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
            return [str(x) for x in val] if isinstance(val, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


def domain_of(chunk: dict) -> str:
    return str(payload_of(chunk).get("domain") or "")


def metrics(rows: list, expected: set) -> dict:
    """hit@1/hit@3/MRR по документам-эталонам; учитываются только фрагменты, чей документ найден."""
    rank = None
    for i, c in enumerate(rows, 1):
        did = doc_id_of(c)
        if did in expected:
            rank = i
            break
    return {"hit@1": 1.0 if rank == 1 else 0.0,
            "hit@3": 1.0 if rank and rank <= 3 else 0.0,
            "hit@10": 1.0 if rank and rank <= 10 else 0.0,
            "mrr": (1.0 / rank) if rank else 0.0,
            "найден": bool(rank), "ранг": rank}


async def main() -> int:
    from src.api.services.chat_service import chat_service
    from src.indexing import document_topics as dt
    from src.indexing.embeddings_service import embeddings_service

    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default="/app/data/eval_questions_v3.json")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    data = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    qs = data if isinstance(data, list) else (data.get("questions") or [])
    qs = [q for q in qs if q.get("relevant_document_ids")]
    if args.limit:
        qs = qs[: args.limit]
    print(f"вопросов с эталонами: {len(qs)} (top-{args.top} для отбора, метрики по top-10)\n")

    modes = {"legacy": [], "rubric": [], "нет фильтра": []}
    per_question = []
    for q in qs:
        query = str(q.get("query") or "")
        expected = set(q.get("relevant_document_ids") or [])
        # Значение темы/домена — то же, что возьмёт чат
        try:
            qa = await chat_service._detect_query_analysis(query)
            domain_value = (qa or {}).get("domain")
        except Exception as e:  # noqa: BLE001
            domain_value = None
            print(f"   (классификатор не ответил: {type(e).__name__})")
        rubric = dt.rubric_for_legacy(domain_value)

        rows = await embeddings_service.search(query=query, limit=args.top)
        legacy_rows = [c for c in rows if domain_of(c) in ("", str(domain_value or ""))] \
            if domain_value else rows
        rubric_rows = [c for c in rows if not rubric or not rubrics_of(c) or rubric in rubrics_of(c)]

        m_legacy = metrics(legacy_rows[:10], expected)
        m_rubric = metrics(rubric_rows[:10], expected)
        m_none = metrics(rows[:10], expected)
        modes["legacy"].append(m_legacy)
        modes["rubric"].append(m_rubric)
        modes["нет фильтра"].append(m_none)
        per_question.append({"query": query[:70], "domain": domain_value, "rubric": rubric,
                             "legacy": m_legacy["ранг"], "rubric_ранг": m_rubric["ранг"],
                             "без_фильтра": m_none["ранг"]})

    print(f"{'режим':<14}{'hit@1':>8}{'hit@3':>8}{'hit@10':>8}{'MRR':>8}{'не найден':>11}")
    for name, ms in modes.items():
        n = len(ms) or 1
        print(f"{name:<14}{sum(m['hit@1'] for m in ms)/n:>8.3f}"
              f"{sum(m['hit@3'] for m in ms)/n:>8.3f}{sum(m['hit@10'] for m in ms)/n:>8.3f}"
              f"{sum(m['mrr'] for m in ms)/n:>8.3f}"
              f"{sum(1 for m in ms if not m['найден']):>11}")

    print("\nпо вопросам (ранг эталона: legacy / rubric / без фильтра):")
    for r in per_question:
        print(f"   {r['domain'] or '—':<12} {str(r['rubric'] or '—'):<10} "
              f"{str(r['legacy']):>4} / {str(r['rubric_ранг']):>4} / {str(r['без_фильтра']):>4}   {r['query']}")
    out = Path("/app/data/theme_filter_ab.json")
    out.write_text(json.dumps({"per_question": per_question}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\nподробно: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
