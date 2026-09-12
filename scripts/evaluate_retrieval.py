#!/usr/bin/env python3
"""Оценка качества retrieval (поиска) KAG.

Считает стандартные метрики поиска: Hit@K, Recall@K, Precision@K, MRR,
а также (если размечен уровень пунктов) chunk-level попадания.

Зачем: без метрик невозможно понять, стало ли лучше от изменения чанкинга,
эмбеддингов, нормализации терминов или payload-фильтров. Скрипт даёт
воспроизводимую цифру до/после.

Использование:
    # 1. Посмотреть список документов, чтобы составить эталонный набор
    python scripts/evaluate_retrieval.py --list-documents

    # 2. Создать шаблон набора вопросов
    python scripts/evaluate_retrieval.py --init-sample

    # 3. Прогнать оценку (нужен запущенный API)
    python scripts/evaluate_retrieval.py --questions scripts/eval_questions.json

    # На сервере 18 (API только на 127.0.0.1):
    python3 scripts/evaluate_retrieval.py --url http://localhost:8000

Разметка релевантности:
  - relevant_document_ids — хотя бы один из этих документов в топ-K = попадание
    (документный уровень, основной режим);
  - relevant_chunk_ids — если размечен, дополнительно считается chunk-level
    (точное попадание в конкретный чанк).

Никаких внешних зависимостей — только stdlib.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_URL = "http://localhost:8000"
DEFAULT_QUESTIONS = "scripts/eval_questions.json"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _request(url: str, payload: Optional[dict], token: Optional[str],
             insecure: bool, timeout: float = 120.0) -> Any:
    """POST/GET JSON. payload=None → GET."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    ctx = None
    if insecure and url.lower().startswith("https"):
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise SystemExit(f"HTTP {e.code} от {url}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Не удалось подключиться к {url}: {e.reason}\n"
                         f"API запущен? На сервере 18 — http://localhost:8000")


def search(base_url: str, query: str, limit: int, token: Optional[str],
           insecure: bool) -> List[Dict[str, Any]]:
    data = _request(f"{base_url.rstrip('/')}/api/v1/chat/search",
                    {"query": query, "limit": limit}, token, insecure)
    return data.get("chunks", []) or []


def list_documents(base_url: str, token: Optional[str], insecure: bool,
                   limit: int = 1000) -> List[Dict[str, Any]]:
    data = _request(f"{base_url.rstrip('/')}/api/v1/upload/list?limit={limit}",
                    None, token, insecure)
    if isinstance(data, dict):
        for key in ("documents", "items", "results", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------
# Метрики
# --------------------------------------------------------------------------

def _ranks(hits: List[Dict[str, Any]], relevant_docs: set, relevant_chunks: set) -> Dict[str, Any]:
    """Позиции попаданий (1-based) по документам и чанкам."""
    doc_ranks, chunk_ranks = [], []
    for pos, hit in enumerate(hits, start=1):
        doc_id = str(hit.get("document_id") or "")
        chunk_id = str(hit.get("chunk_id") or "")
        if doc_id and doc_id in relevant_docs:
            doc_ranks.append(pos)
        if relevant_chunks and chunk_id and chunk_id in relevant_chunks:
            chunk_ranks.append(pos)
    return {"doc_ranks": doc_ranks, "chunk_ranks": chunk_ranks}


def evaluate_question(base_url: str, q: Dict[str, Any], k_values: List[int],
                      token: Optional[str], insecure: bool,
                      search_limit: int) -> Dict[str, Any]:
    query = q["query"]
    rel_docs = {str(x) for x in (q.get("relevant_document_ids") or [])}
    rel_chunks = {str(x) for x in (q.get("relevant_chunk_ids") or [])}
    if not rel_docs and not rel_chunks:
        raise SystemExit(f"Вопрос без разметки релевантности: {query!r}")

    hits = search(base_url, query, search_limit, token, insecure)
    ranks = _ranks(hits, rel_docs, rel_chunks)

    result: Dict[str, Any] = {
        "query": query,
        "returned": len(hits),
        "doc_ranks": ranks["doc_ranks"],
        "chunk_ranks": ranks["chunk_ranks"],
        "metrics": {},
        "top": [
            {"rank": i, "document_id": h.get("document_id"), "chunk_id": h.get("chunk_id"),
             "score": round(float(h.get("score") or 0.0), 4)}
            for i, h in enumerate(hits[:10], start=1)
        ],
    }

    # Документный уровень (основной)
    if rel_docs:
        found = len({h.get("document_id") for h in hits if str(h.get("document_id")) in rel_docs})
        for k in k_values:
            hit_k = [r for r in ranks["doc_ranks"] if r <= k]
            result["metrics"][f"hit@{k}"] = 1.0 if hit_k else 0.0
            result["metrics"][f"recall@{k}"] = min(1.0, len(hit_k) / len(rel_docs))
            result["metrics"][f"precision@{k}"] = (len(hit_k) / k) if hits else 0.0
        result["metrics"]["mrr"] = (1.0 / ranks["doc_ranks"][0]) if ranks["doc_ranks"] else 0.0
        result["relevant_found"] = found
        result["relevant_total"] = len(rel_docs)

    # Chunk-уровень (если размечен)
    if rel_chunks:
        for k in k_values:
            hit_k = [r for r in ranks["chunk_ranks"] if r <= k]
            result["metrics"][f"chunk_recall@{k}"] = min(1.0, len(hit_k) / len(rel_chunks))
        result["metrics"]["chunk_mrr"] = (1.0 / ranks["chunk_ranks"][0]) if ranks["chunk_ranks"] else 0.0

    return result


def aggregate(results: List[Dict[str, Any]]) -> Dict[str, float]:
    """Средние значения метрик по всем вопросам."""
    keys = sorted({k for r in results for k in r["metrics"]})
    agg = {}
    for key in keys:
        vals = [r["metrics"][key] for r in results if key in r["metrics"]]
        if vals:
            agg[key] = round(statistics.fmean(vals), 4)
    return agg


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

SAMPLE = {
    "description": "Эталонный набор вопросов для оценки retrieval KAG. "
                   "Заполните relevant_document_ids (id из --list-documents).",
    "k_values": [1, 3, 5, 10],
    "questions": [
        {"query": "какие требования предъявляются к средствам защиты информации?",
         "relevant_document_ids": ["ВСТАВЬТЕ_ID_ДОКУМЕНТА"],
         "relevant_chunk_ids": [],
         "notes": "пример: заполнить по реальному документу"},
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Оценка качества retrieval KAG")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"базовый URL API (по умолчанию {DEFAULT_URL})")
    ap.add_argument("--token", default=None, help="Bearer-токен (если включена авторизация)")
    ap.add_argument("--insecure", action="store_true", help="не проверять TLS (self-signed HTTPS)")
    ap.add_argument("--questions", default=DEFAULT_QUESTIONS, help="файл с эталонным набором")
    ap.add_argument("--top-k", type=int, default=10, help="сколько результатов запрашивать у поиска")
    ap.add_argument("--output", default=None, help="сохранить отчёт в JSON (для сравнения версий)")
    ap.add_argument("--list-documents", action="store_true", help="вывести документы (для разметки набора)")
    ap.add_argument("--init-sample", action="store_true", help="создать шаблон файла набора")
    args = ap.parse_args()

    if args.init_sample:
        path = Path(args.questions)
        if path.exists():
            print(f"{path} уже существует — не перезаписываю")
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(SAMPLE, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Шаблон создан: {path}")
        return 0

    if args.list_documents:
        docs = list_documents(args.url, args.token, args.insecure)
        print(f"Документов: {len(docs)}\n")
        for d in docs:
            if not isinstance(d, dict):
                continue
            print(f"  {d.get('id') or d.get('document_id')}  "
                  f"[{d.get('status', '?')}]  {d.get('filename') or d.get('name', '')}")
        return 0

    path = Path(args.questions)
    if not path.exists():
        print(f"Нет файла набора: {path}\nСоздайте шаблон: --init-sample")
        return 1
    spec = json.loads(path.read_text(encoding="utf-8"))
    questions = spec.get("questions") or []
    k_values = spec.get("k_values") or [1, 3, 5, 10]
    if not questions:
        print("В наборе нет вопросов")
        return 1

    print(f"Оценка retrieval: {len(questions)} вопросов, top-K={args.top_k}, API={args.url}\n")
    results = [evaluate_question(args.url, q, k_values, args.token, args.insecure, args.top_k)
               for q in questions]

    for r in results:
        m = r["metrics"]
        miss = "ПРОМАХ" if m.get("hit@10", m.get("hit@3", 1.0)) == 0.0 else "ok"
        print(f"[{miss}] {r['query'][:70]}")
        print(f"        {', '.join(f'{k}={v:.3f}' for k, v in sorted(m.items()))}")
        print(f"        вернулось: {r['returned']}, ранги релевантных: {r['doc_ranks']}")

    agg = aggregate(results)
    print("\n=== СРЕДНИЕ МЕТРИКИ ===")
    for k, v in sorted(agg.items()):
        print(f"  {k:16s} {v:.4f}")

    report = {"url": args.url, "questions_file": str(path), "top_k": args.top_k,
              "count": len(results), "aggregate": agg, "results": results}
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nОтчёт сохранён: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
