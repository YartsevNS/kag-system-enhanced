"""Оценка качества RAG судьёй-моделью (LLM-as-judge) — четыре метрики.

Почему свои промпты, а не ragas: ragas тянет langchain и datasets в venv, где живёт api;
наши метрики считаются четырьмя короткими вызовами дешёвой модели (deepseek-v4-flash-0731 на
отдельном ключе судьи), промпты видны и правятся, стоит это копейки (замер: 0,003 ₽ за вызов).
Метрики те же, что в RAGAS:
  faithfulness      — ответ опирается только на найденные фрагменты (нет выдумок);
  answer_relevance  — ответ отвечает на заданный вопрос;
  context_precision — какая доля найденных фрагментов относится к вопросу;
  context_recall    — есть ли в найденных фрагментах всё нужное для эталонного ответа.

Запуск (ключ судьи остаётся на ноутбуке):
    python scripts/eval_rag_judge.py --collected eval/collected_answers.json
    python scripts/eval_rag_judge.py --collected ... --limit 3      # быстрая проверка

Переменные: POLZA_KEY_FILE (по умолчанию ключ судьи), JUDGE_MODEL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

KEY_FILE = Path(os.environ.get("POLZA_KEY_FILE",
                               r"C:\VSCODE_PROJECT\keys\polza_deepseec судья.txt"))
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "deepseek/deepseek-v4-flash-0731")
URL = "https://polza.ai/api/v1/chat/completions"
ROOT = Path(__file__).resolve().parents[1]

SYSTEM = ("Ты — строгий эксперт по оценке ответов системы поиска по документам (RAG). "
          "Отвечай ТОЛЬКО одним JSON-объектом без пояснений вокруг: "
          '{"score": <число от 0 до 1>, "reason": "<кратко, до 200 символов>"}. '
          "Оценка 1.0 — безупречно, 0.0 — полностью неверно. Не добавляй поля, не пиши текст вне JSON.")


def api_key() -> str:
    if not KEY_FILE.exists():
        raise SystemExit(f"нет файла с ключом судьи: {KEY_FILE}")
    return KEY_FILE.read_text(encoding="utf-8").strip()


def ask(prompt: str, key: str, max_tokens: int = 900) -> tuple[dict, float, int]:
    """Один вызов судьи. Возвращает (разобранный JSON, стоимость ₽, токенов)."""
    body = json.dumps({
        "model": JUDGE_MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    text = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    usage = d.get("usage") or {}
    cost = float(usage.get("cost") or 0) or 0.0
    tokens = int(usage.get("total_tokens") or 0)

    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"score": None, "reason": f"судья вернул не JSON: {text[:120]!r}"}, cost, tokens
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"score": None, "reason": f"JSON не разобран: {m.group(0)[:120]!r}"}, cost, tokens
    try:
        parsed["score"] = None if parsed.get("score") is None else round(float(parsed["score"]), 3)
    except (TypeError, ValueError):
        parsed["score"] = None
    return parsed, cost, tokens


def _ctx_block(contexts: list[dict], limit: int = 6, chars: int = 1200) -> str:
    out = []
    for i, c in enumerate(contexts[:limit], 1):
        out.append(f"[Фрагмент {i}] {c.get('filename') or '?'} | score={c.get('score')}\n"
                   f"{(c.get('content') or '')[:chars]}")
    return "\n\n".join(out) if out else "(фрагменты не найдены)"


def judge_item(item: dict, collected: dict, key: str) -> tuple[dict, float, int]:
    q = item["question"]
    ans = collected.get("answer") or ""
    ctx = collected.get("contexts") or []
    gt = item.get("ground_truth")
    spend = 0.0
    tokens = 0
    res: dict = {}

    # 1. Faithfulness: можно ли вывести каждое утверждение ответа из фрагментов.
    p = (f"ВОПРОС:\n{q}\n\nНАЙДЕННЫЕ ФРАГМЕНТЫ:\n{_ctx_block(ctx)}\n\n"
         f"ОТВЕТ СИСТЕМЫ:\n{ans[:4000]}\n\n"
         "Оцени долю утверждений ответа, которые опираются на фрагменты. Если ответ честно "
         "говорит «в документах нет данных», ставь 1.0 (это верное поведение, а не выдумка).")
    r, c, t = ask(p, key); res["faithfulness"] = r; spend += c; tokens += t

    # 2. Answer relevance: отвечает ли ответ на вопрос (даже если факта нет в базе).
    p = (f"ВОПРОС:\n{q}\n\nОТВЕТ СИСТЕМЫ:\n{ans[:4000]}\n\n"
         "Оцени, насколько ответ отвечает именно на этот вопрос: полнота по сути, отсутствие "
         "посторонних тем. Верный отказ «такой информации в документах нет» без выдумок оцени "
         "не ниже 0.6: пользователю важен честный ответ, а не догадка.")
    r, c, t = ask(p, key); res["answer_relevance"] = r; spend += c; tokens += t

    # 3. Context precision: доля фрагментов, полезных для вопроса.
    p = (f"ВОПРОС:\n{q}\n\nФРАГМЕНТЫ (по порядку):\n{_ctx_block(ctx, limit=10, chars=700)}\n\n"
         "Оцени, какая доля найденных фрагментов нужна для ответа на вопрос. 1.0 — все полезны, "
         "0.0 — все посторонние. Посторонним считается фрагмент не на тему вопроса.")
    r, c, t = ask(p, key); res["context_precision"] = r; spend += c; tokens += t

    # 4. Context recall: есть ли в фрагментах всё нужное (нужен эталон).
    if not gt:
        p = (f"ВОПРОС:\n{q}\n\nФРАГМЕНТЫ:\n{_ctx_block(ctx)}\n\n"
             "Если в фрагментах есть прямой ответ на вопрос — верни его одной-двумя фразами "
             'как ответ ({"score": 1, "reason": "<ответ>"}). Если ответа во фрагментах нет — '
             'верни {"score": 0, "reason": "в фрагментах ответа нет"}.')
        r, c, t = ask(p, key); spend += c; tokens += t
        res["ground_truth_extracted"] = r.get("reason") if r.get("score") else None
        gt = res["ground_truth_extracted"]

    if gt:
        p = (f"ВОПРОС:\n{q}\n\nЭТАЛОННЫЙ ОТВЕТ:\n{gt}\n\nФРАГМЕНТЫ:\n{_ctx_block(ctx)}\n\n"
             "Оцени, какая доля информации эталонного ответа присутствует в фрагментах "
             "(не в ответе системы, а в самих фрагментах).")
        r, c, t = ask(p, key); res["context_recall"] = r; spend += c; tokens += t
    else:
        res["context_recall"] = {"score": None, "reason": "эталон не определён"}

    return res, spend, tokens


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collected", default=str(ROOT / "eval" / "collected_answers.json"))
    ap.add_argument("--golden", default=str(ROOT / "eval" / "golden_dataset.jsonl"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    key = api_key()
    golden = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]
    collected = json.loads(Path(args.collected).read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in collected}
    if args.limit:
        golden = golden[:args.limit]

    print(f"судья: {JUDGE_MODEL} | вопросов: {len(golden)}")
    rows = []
    total_cost = 0.0
    total_tokens = 0
    for item in golden:
        c = by_id.get(item["id"])
        if not c:
            print(f"  {item['id']}: нет собранного ответа — пропуск")
            continue
        res, cost, tokens = judge_item(item, c, key)
        total_cost += cost
        total_tokens += tokens
        def sc(name):
            v = res.get(name, {})
            return v.get("score") if isinstance(v, dict) else None
        rows.append({"id": item["id"], "question": item["question"][:70],
                     "faithfulness": sc("faithfulness"), "answer_relevance": sc("answer_relevance"),
                     "context_precision": sc("context_precision"), "context_recall": sc("context_recall"),
                     "reasons": {k: v.get("reason") for k, v in res.items() if isinstance(v, dict)},
                     "answer_chars": len(c.get("answer") or ""),
                     "contexts": len(c.get("contexts") or [])})
        print(f"  {item['id']}: faith={sc('faithfulness')} rel={sc('answer_relevance')} "
              f"prec={sc('context_precision')} rec={sc('context_recall')} "
              f"({len(c.get('answer') or '')} симв.)", flush=True)

    def avg(name):
        vals = [r[name] for r in rows if r[name] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {m: avg(m) for m in ("faithfulness", "answer_relevance",
                                   "context_precision", "context_recall")}
    summary["вопросов"] = len(rows)
    summary["стоимость_₽"] = round(total_cost, 4)
    summary["токенов_судьи"] = total_tokens

    print("\n=== ИТОГ ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    out = Path(args.out) if args.out else ROOT / "reports" / "eval" / f"rag_eval_{datetime.now():%Y%m%d_%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "model": JUDGE_MODEL, "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  отчёт: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
