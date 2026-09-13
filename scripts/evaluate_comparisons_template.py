"""Оценка сравнительных ответов: покрытие сравниваемых документов.

Для сравнительных вопросов нет эталонных ответов, поэтому мерим то, что прямо отражает цель
режима: сколько из сравниваемых документов действительно учтено — встречается в источниках
ответа ИЛИ упомянуто в тексте ответа (по обозначению документа).

Запуск (в контейнере api): docker exec -e KAG_TOKEN=<токен> -i kag-api python - < этот_файл
"""
import asyncio
import json
import os
import re
import urllib.request

from src.api.services.document_repository import get_doc_repo

API = os.environ.get("KAG_API", "http://localhost:8000/api/v1")
TOKEN = os.environ.get("KAG_TOKEN", "")
QUESTIONS = __QUESTIONS__  # noqa: F821  (подставляется генератором)


def post_json(path: str, payload: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def mentions(answer: str, card: dict) -> bool:
    """Упомянут ли документ в ответе: по обозначению/названию/файлу."""
    a = (answer or "").lower()
    for field in ("title", "filename", "source"):
        value = str(card.get(field) or "").strip()
        if not value:
            continue
        # обозначение вида «Р 1323565.1.030—2020» ищем и с дефисами других начертаний
        core = re.sub(r"[—–−]", "-", value.lower())
        if core and (core in re.sub(r"[—–−]", "-", a) or value.lower()[:25] in a):
            return True
    return False


async def main() -> None:
    print(f"  сравнительных вопросов: {len(QUESTIONS)}")
    # названия документов берём из БД: по ним проверяем, упомянуты ли они в ответе
    all_docs = get_doc_repo().get_all() or {}

    def _cards_for(ids):
        out = []
        for did in ids:
            d = all_docs.get(did) or {}
            out.append({"card_document_id": did,
                        "title": d.get("recognized_title") or "",
                        "filename": d.get("filename") or ""})
        return out

    rows = []
    for i, q in enumerate(QUESTIONS, 1):
        question = q["query"]
        cards = _cards_for(q.get("document_ids") or [])
        try:
            resp = post_json("/chat/", {"messages": [{"role": "user", "content": question}],
                                        "stream": False, "temperature": 0.0})
            answer = str(resp.get("response") or resp.get("answer") or "")
            sources = resp.get("sources") or []
        except Exception as e:
            answer, sources = "", []
            print(f"  {i}. ОШИБКА: {type(e).__name__}: {e}")
        src_ids = {str(s.get("document_id")) for s in sources if isinstance(s, dict)}
        cited = [c for c in cards if mentions(answer, c)]
        cited += [c for c in cards if c.get("card_document_id") in src_ids and c not in cited]
        coverage = len(cited) / max(1, len(cards))
        rows.append({"query": question, "coverage": coverage,
                     "docs": len(cards), "cited": len(cited),
                     "answer_len": len(answer), "answer": answer})
        print(f"  {i}. покрытие {len(cited)}/{len(cards)} = {coverage:.2f} | "
              f"ответ {len(answer):5d} симв. | {question[:52]}")

    if not rows:
        return
    avg = sum(r["coverage"] for r in rows) / len(rows)
    full = sum(1 for r in rows if r["coverage"] >= 1.0)
    print()
    print(f"  СРЕДНЕЕ ПОКРЫТИЕ: {avg:.4f} | полностью покрыты оба документа: {full} из {len(rows)}")
    for r in rows:
        print()
        print(f"  [{r['coverage']:.2f}] {r['query'][:90]}")
        print(f"    ответ: {r['answer'][:500]!r}")
    print()
    print("  COMPARISON_JSON=" + json.dumps(
        [{k: v for k, v in r.items() if k != "answer"} for r in rows], ensure_ascii=False))


asyncio.run(main())
