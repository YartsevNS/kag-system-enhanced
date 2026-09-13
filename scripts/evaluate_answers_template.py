"""Оценка качества ОТВЕТА чата против эталонных ответов владельца.

Почему отдельно от evaluate_retrieval.py: поиск измеряет hit@K (нашёлся ли документ), а здесь
измеряется, что именно система ответила. Это ближе к цели «быстрые и умные ответы» и позволяет
сравнивать версии (разделы, префиксы, сравнения) по существу, а не только по рангам.

Как работает:
1) для каждого вопроса с эталонным ответом вызывается POST /api/v1/chat/ (тот же путь, что у
   пользователя: поиск + контекст + генерация);
2) ответ судит LLM-судья по рубрике (без RAG в его промпте — только вопрос, эталон и ответ
   системы), возвращает JSON: score 0..1, чего не хватает, что противоречит эталону;
3) печатается таблица и средний балл; слабые ответы (score < 0.6) приводятся целиком для разбора.

Запуск на стенде (в контейнере api, где есть и сеть до localhost:8000, и LLM-клиент):
    docker exec -e KAG_TOKEN=<токен> -i kag-api python - < этот_файл
"""
import asyncio
import json
import os
import urllib.request

from src.api.services.provider_service import provider_service
from src.indexing.entity_extractor import entity_extractor

API = os.environ.get("KAG_API", "http://localhost:8000/api/v1")
TOKEN = os.environ.get("KAG_TOKEN", "")
QUESTIONS = __QUESTIONS__  # noqa: F821  (подставляется генератором)

JUDGE_PROMPT = """Ты — строгий оценщик ответов RAG-системы.

Вопрос пользователя:
{question}

Эталонный ответ (истина):
{expected}

Ответ системы:
{actual}

Оцени, насколько ответ системы соответствует эталону по существу:
1.0 — все существенные факты эталона есть, противоречий нет;
0.7-0.9 — основное верно, но упущены детали;
0.4-0.6 — часть фактов есть, часть упущена или искажена;
0.1-0.3 — по теме, но существенно не то;
0.0 — пусто, не по теме или противоречит эталону.

Верни ТОЛЬКО JSON: {{"score": 0.0, "missing": ["..."], "wrong": ["..."], "comment": "одна фраза"}}"""


def post_json(path: str, payload: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ask_chat(question: str) -> dict:
    return post_json("/chat/", {
        "messages": [{"role": "user", "content": question}],
        "stream": False,
        "temperature": 0.0,
    })


async def judge(question: str, expected: str, actual: str) -> dict:
    cfg = provider_service.get_function_llm_config("doc_analysis") or {}
    model = cfg.get("model") or "deepseek-v4-flash"
    llm_url = cfg.get("url") or "https://api.deepseek.com"
    api_key = cfg.get("api_key") or ""
    provider = cfg.get("provider") or "deepseek"
    prompt = JUDGE_PROMPT.format(question=question, expected=expected, actual=actual[:4000])
    res = await entity_extractor._call_llm(
        prompt, model, llm_url, chunk_id="judge", pass_name="answer_judge",
        api_key=api_key, provider=provider, temperature=0.0,
    )
    if isinstance(res, dict) and "score" in res:
        return res
    return {"score": 0.0, "comment": f"судья не разобрал ответ: {str(res)[:120]}"}


async def main() -> None:
    print(f"  вопросов с эталоном: {len(QUESTIONS)}")
    rows = []
    for i, q in enumerate(QUESTIONS, 1):
        question = q["query"]
        expected = q["expected_answer"]
        try:
            resp = ask_chat(question)
            actual = str(resp.get("response") or resp.get("answer") or resp.get("content") or "")
            sources = resp.get("sources") or resp.get("documents") or []
        except Exception as e:
            actual, sources = "", []
            print(f"  {i}. ОШИБКА чата: {type(e).__name__}: {e}")
        if not actual.strip():
            verdict = {"score": 0.0, "comment": "пустой ответ"}
        else:
            verdict = await judge(question, expected, actual)
        score = float(verdict.get("score") or 0.0)
        rows.append({"id": q.get("id"), "query": question, "score": score,
                     "comment": verdict.get("comment", ""),
                     "missing": verdict.get("missing") or [],
                     "sources": len(sources), "answer_len": len(actual),
                     "answer": actual})
        print(f"  {i:2d}. score={score:.2f} | {question[:58]:60s} | ответ {len(actual):5d} симв.")

    if not rows:
        print("  нет вопросов с эталоном")
        return
    avg = sum(r["score"] for r in rows) / len(rows)
    print()
    print(f"  СРЕДНИЙ БАЛЛ ОТВЕТА: {avg:.4f} (по {len(rows)} вопросам)")
    good = sum(1 for r in rows if r["score"] >= 0.8)
    mid = sum(1 for r in rows if 0.6 <= r["score"] < 0.8)
    bad = [r for r in rows if r["score"] < 0.6]
    print(f"  >=0.8: {good} | 0.6-0.8: {mid} | <0.6: {len(bad)}")
    for r in bad:
        print()
        print(f"  СЛАБЫЙ: [{r['score']:.2f}] {r['query'][:80]}")
        print(f"    судья: {r['comment'][:200]}")
        if r["missing"]:
            print(f"    упущено: {r['missing'][:4]}")
        print(f"    ответ: {r['answer'][:300]!r}")
    print()
    print("  ANSWER_EVAL_JSON=" + json.dumps(
        [{k: v for k, v in r.items() if k != "answer"} for r in rows], ensure_ascii=False))


asyncio.run(main())
