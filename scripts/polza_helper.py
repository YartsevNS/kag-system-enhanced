"""Помощник через polza.ai: дешёвые модели для проверок, судейства и разбора.

Зачем: свои замеры и проверки не должны зависеть от того, отвечает ли модель на
стенде. Ключ REST-API лежит у пользователя (файл api.txt), в вывод НЕ печатается.

Возможности:
  python scripts/polza_helper.py balance
  python scripts/polza_helper.py ask "вопрос" [--model deepseek/deepseek-v4-flash-0731] [--max-tokens 400]
  python scripts/polza_helper.py judge --question "..." --expected "..." --answer "..."

Деньги: каждый вызов пишет строку в журнал (reports/_scratch/polza_costs.jsonl) со
стоимостью из ответа (usage.cost_rub), а перед вызовом проверяется дневной лимит
(POLZA_DAILY_LIMIT_RUB, по умолчанию 50 ₽). Превышение — отказ, а не тихая трата.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KEY_FILE = Path(os.environ.get("POLZA_KEY_FILE", r"C:\VSCODE_PROJECT\keys\api.txt"))
BASE = os.environ.get("POLZA_BASE_URL", "https://polza.ai/api/v1")
LOG = Path(os.environ.get("POLZA_COST_LOG", "reports/_scratch/polza_costs.jsonl"))
DAILY_LIMIT = float(os.environ.get("POLZA_DAILY_LIMIT_RUB", "50"))
DEFAULT_MODEL = os.environ.get("POLZA_MODEL", "deepseek/deepseek-v4-flash-0731")
# Для судейства дешёвая reasoning-модель v4-flash-0731 не годится: она тратит весь
# лимит токенов на «размышление» и возвращает пустой content (поймано на живом
# вызове). glm-5.3-flash и v4.1-flash отдают корректный JSON за ~0.013-0.019 ₽.
DEFAULT_JUDGE_MODEL = os.environ.get("POLZA_JUDGE_MODEL", "deepseek/deepseek-v4.1-flash")

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

Верни ТОЛЬКО JSON: {"score": 0.0, "missing": ["..."], "wrong": ["..."], "comment": "одна фраза"}"""


def key() -> str:
    """REST-ключ из файла пользователя.

    В ключе есть символы `_` и `-`, поэтому берём либо всю строку, начинающуюся с
    `pza_`, либо (для строк вида «подпись + ключ») ищем полный токен вместе с
    подчёркиваниями и дефисами. Обрезанный ключ даёт 401 — так уже было.
    """
    lines = [ln.strip() for ln in KEY_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for line in lines:
        if line.startswith("pza_"):
            return line
    for line in lines:
        m = re.search(r"(pza_[A-Za-z0-9_\-]+)", line)
        if m:
            return m.group(1)
    sys.exit(f"не нашёл REST-ключ (pza_…) в {KEY_FILE}")


def spent_today() -> float:
    if not LOG.exists():
        return 0.0
    today = datetime.now(timezone.utc).date().isoformat()
    total = 0.0
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if str(rec.get("ts", "")).startswith(today):
            total += float(rec.get("cost_rub") or 0)
    return total


def log_call(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def api(path: str, payload: dict | None = None, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET",
                                 headers={"Authorization": "Bearer " + key(),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def chat(prompt: str, model: str, max_tokens: int, purpose: str) -> tuple[str, float]:
    used = spent_today()
    if used >= DAILY_LIMIT:
        sys.exit(f"дневной лимит исчерпан: потрачено {used:.2f} ₽ из {DAILY_LIMIT:.2f} ₽")
    res = api("/chat/completions", {"model": model, "max_tokens": max_tokens, "temperature": 0.0,
                                    "messages": [{"role": "user", "content": prompt}]})
    answer = (res.get("choices") or [{}])[0].get("message", {}).get("content", "")
    usage = res.get("usage") or {}
    cost = float(usage.get("cost_rub") or 0)
    log_call({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": model,
              "purpose": purpose, "prompt_chars": len(prompt), "answer_chars": len(str(answer)),
              "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
              "cost_rub": cost})
    return str(answer), cost


def main() -> int:
    ap = argparse.ArgumentParser(description="Помощник polza.ai (дешёвые модели для проверок)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("balance", help="показать баланс")
    p_ask = sub.add_parser("ask", help="спросить модель")
    p_ask.add_argument("prompt")
    p_ask.add_argument("--model", default=DEFAULT_MODEL)
    p_ask.add_argument("--max-tokens", type=int, default=600)
    p_ask.add_argument("--purpose", default="ask")

    p_j = sub.add_parser("judge", help="оценить ответ системы против эталона")
    p_j.add_argument("--question", required=True)
    p_j.add_argument("--expected", required=True)
    p_j.add_argument("--answer", required=True)
    p_j.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    p_j.add_argument("--max-tokens", type=int, default=int(os.environ.get("POLZA_JUDGE_MAX_TOKENS", "900")))

    args = ap.parse_args()

    if args.cmd == "balance":
        b = api("/balance")
        print(f"  доступно: {b.get('available')} ₽ | всего: {b.get('amount')} ₽ | "
              f"потрачено: {b.get('spentAmount')} ₽ | обновлено: {b.get('updatedAt')}")
        print(f"  по журналу сегодня: {spent_today():.4f} ₽ (лимит {DAILY_LIMIT:.2f} ₽)")
        return 0

    if args.cmd == "ask":
        t = time.time()
        answer, cost = chat(args.prompt, args.model, args.max_tokens, args.purpose)
        print(f"  [{args.model}] {time.time() - t:.1f} с, {cost:.5f} ₽")
        print(answer[:2000])
        return 0

    prompt = (JUDGE_PROMPT.replace("{question}", args.question)
                          .replace("{expected}", args.expected)
                          .replace("{actual}", args.answer[:4000]))
    answer, cost = chat(prompt, args.model, args.max_tokens, "judge")
    text = re.sub(r"```[a-zA-Z]*", "", answer).replace("```", "").strip()
    verdict = {}
    try:
        verdict = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                verdict = json.loads(m.group(0))
            except Exception:
                verdict = {"score": None, "comment": text[:120]}
    print(f"  судья [{args.model}]: score={verdict.get('score')} за {cost:.5f} ₽")
    if verdict.get("score") is None:
        # Пустой или неразбираемый ответ — показываем сырой текст, иначе непонятно,
        # это модель не ответила или разбор сломался.
        print(f"  сырой ответ ({len(answer)} симв.): {answer[:300]!r}")
    print(f"  комментарий: {str(verdict.get('comment'))[:200]}")
    if verdict.get("missing"):
        print(f"  упущено: {str(verdict.get('missing'))[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
