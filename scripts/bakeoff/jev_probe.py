"""Опыт с моделью JEV (TypeSafe System One) через Polza: закрытые вопросы по нашему тексту.

Зачем: проверить на реальных фрагментах из нашей базы приём «закрытые вопросы с вероятностями»
вместо свободной генерации. Нам это нужно для типизации связей и разметки вида/рубрики документа —
там, где ответ обязан быть из заранее заданного списка, а не придуман моделью.

Что делает: берёт файл запроса (state + questions), отправляет в /api/v1/systemone, печатает ответы
таблицей и расход; при --chat-model дополнительно задаёт тот же вопрос обычной чат-модели (сравнение).

Запуск (ключ ТОЛЬКО из окружения, в аргументах не передавать):
  export POLZA_API_KEY=...
  python scripts/bakeoff/jev_probe.py reports/_scratch/jev_a_standard.json
  python scripts/bakeoff/jev_probe.py q.json --chat-model openai/gpt-5-nano
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://polza.ai/api/v1/systemone"
CHAT_URL = "https://polza.ai/api/v1/chat/completions"


def _post(url: str, body: dict, key: str, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _print_answers(data: dict) -> None:
    print(f"модель: {data.get('model')}")
    for name, ans in (data.get("answers") or {}).items():
        kind = ans.get("type")
        if kind == "choice":
            probs = ", ".join(f"{k}={v}" for k, v in (ans.get("probabilities") or {}).items() if v)
            print(f"  {name}: {ans.get('choice')}  (уверенность {ans.get('confidence')})  [{probs}]")
        elif kind == "noul":
            print(f"  {name}: вероятность «да» = {ans.get('noul')}")
        elif kind == "score":
            print(f"  {name}: {ans.get('score')}  (уверенность {ans.get('confidence')})")
        else:
            print(f"  {name}: {json.dumps(ans, ensure_ascii=False)}")
    usage = data.get("usage") or {}
    print(f"  расход: вход {usage.get('input_tokens')} ток., выход {usage.get('output_tokens')} ток., "
          f"{usage.get('cost_rub')} руб.")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    key = os.environ.get("POLZA_API_KEY") or os.environ.get("HERMES_CUSTOM_POLZA_API_KEY")
    if not key:
        print("нет ключа: задайте POLZA_API_KEY в окружении (в аргументах ключ не передаём)")
        return 2

    path = Path(sys.argv[1])
    body = json.loads(path.read_text(encoding="utf-8"))
    body.setdefault("model", "typesafe/jev")

    chat_model = None
    if "--chat-model" in sys.argv:
        chat_model = sys.argv[sys.argv.index("--chat-model") + 1]

    t0 = time.time()
    try:
        data = _post(URL, body, key)
    except urllib.error.HTTPError as e:
        print(f"ОТКАЗ {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        return 1
    elapsed = time.time() - t0

    print(f"=== JEV (запрос {path.name}, {elapsed:.2f} с) ===")
    _print_answers(data)
    out = path.with_suffix(".ответ.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  сырой ответ сохранён: {out}")

    if chat_model:
        text = body.get("state") or ""
        questions = body.get("questions") or {}
        ask = (text + "\n\nВопрос: " + "; ".join(
            f"{k}: {v.get('instructions', '')} (варианты: {', '.join((v.get('criteria') or {}).keys())})"
            if isinstance(v.get("criteria"), dict) else f"{k}: {v.get('instructions', '')}"
            for k, v in questions.items()) + "\nОтветь ТОЛЬКО значениями, без пояснений.")
        t0 = time.time()
        try:
            chat = _post(CHAT_URL, {"model": chat_model, "temperature": 0,
                                    "messages": [{"role": "user", "content": ask}]}, key, timeout=180)
        except urllib.error.HTTPError as e:
            print(f"=== чат-модель {chat_model}: ОТКАЗ {e.code}: {e.read().decode('utf-8','replace')[:200]}")
            return 0
        print(f"\n=== для сравнения, чат-модель {chat_model} ({time.time()-t0:.2f} с) ===")
        choice = ((chat.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        print("  ответ:", repr(choice[:300]))
        u = chat.get("usage") or {}
        print("  расход:", json.dumps(u, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
