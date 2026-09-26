"""Сбор ответов и контекстов для оценки качества RAG (запускается НА СТЕНДЕ).

Зачем отдельный шаг: судью зовём с ноутбука (ключ остаётся там), а стенд отдаёт только
ответы и найденные фрагменты. Так ключ не попадает на сервер и не светится в процессах.

Запуск на стенде:
    docker exec kag-api python /app/data/eval_collect.py /app/data/golden_dataset.jsonl

Результат: /app/data/collected_answers.json (копируем на ноутбук через scp).
Каждая запись несёт trace_id ответа (заголовок X-Trace-ID) — разбор слабого ответа идёт
по журналу, а не по догадкам.
"""
import json
import os
import sys
import urllib.request

API = os.environ.get("KAG_API", "http://localhost:8000")


def _post(path: str, payload: dict, token: str = "") -> tuple[dict, str]:
    """POST + код трассировки из заголовка ответа (X-Trace-ID)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(API + path, data=data,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=180) as r:
        return (json.loads(r.read().decode("utf-8", "replace")),
                (r.headers.get("X-Trace-ID") or ""))


def login() -> str:
    pw = ""
    for path in ("/tmp/.kag_pass", "/app/data/.kag_pass"):
        try:
            pw = open(path).read().strip()
            break
        except Exception:
            continue
    if not pw:
        pw = os.environ.get("ADMIN_PASSWORD", "")
    d, _ = _post("/api/v1/auth/login", {"username": os.environ.get("ADMIN_USER", "admin"),
                                        "password": pw})
    return d.get("access_token") or d.get("token") or ""


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/app/data/golden_dataset.jsonl"
    out = sys.argv[2] if len(sys.argv) > 2 else "/app/data/collected_answers.json"

    items = [json.loads(line) for line in open(src, encoding="utf-8") if line.strip()]
    print(f"вопросов в эталонном наборе: {len(items)}", flush=True)

    token = login()
    print(f"вход: {'ок' if token else 'НЕ УДАЛСЯ'}", flush=True)

    collected = []
    for i, item in enumerate(items, 1):
        q = item["question"]
        trace = ""
        try:
            # Ручка чата ждёт именно список messages: запрос с полем message отдаёт 422.
            ans, trace = _post("/api/v1/chat/",
                               {"messages": [{"role": "user", "content": q}], "context_limit": 10},
                               token)
            answer = ans.get("response") or ""
            sources = ans.get("sources") or []
            model = ans.get("model")
            usage = ans.get("usage") or {}
        except Exception as e:
            answer, sources, model, usage = f"ОШИБКА: {e}", [], None, {}

        # Контексты тем же поиском, что использует чат: нужны для faithfulness/precision.
        contexts = []
        try:
            srch, _ = _post("/api/v1/chat/search", {"query": q, "limit": 10}, token)
            for c in (srch.get("chunks") or []):
                contexts.append({
                    "id": c.get("id") or c.get("chunk_id"),
                    "filename": c.get("filename") or c.get("source_file"),
                    "score": c.get("score"),
                    "content": (c.get("content") or "")[:4000],
                })
        except Exception as e:
            print(f"  {item['id']}: поиск контекстов не сработал: {e}", flush=True)

        collected.append({
            "id": item["id"], "question": q, "answer": answer,
            "model": model, "usage": usage, "trace_id": trace,
            "sources": [{"filename": s.get("filename") or s.get("source_file"),
                         "score": s.get("score"),
                         "document_id": s.get("document_id")} for s in sources],
            "contexts": contexts,
        })
        print(f"  {i}/{len(items)} {item['id']}: ответ {len(answer)} симв., "
              f"источников {len(sources)}, контекстов {len(contexts)}, trace={trace or '—'}",
              flush=True)

    with open(out, "w", encoding="utf-8") as f:
        json.dump(collected, f, ensure_ascii=False, indent=2)
    print(f"сохранено: {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
