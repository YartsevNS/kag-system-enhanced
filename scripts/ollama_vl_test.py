"""Проверка модели зрения в Ollama на реальном изображении.

Запуск на сервере моделей:
    ~/kag-eval/venv/bin/python ollama_vl_test.py <путь к картинке> [модель]

Печатает время, объём, число строк markdown-таблицы и начало ответа — то есть ровно то, что нужно
сравнить с распознаванием через Occular (сетка таблицы) и с exec-путём Qwen2-VL через transformers.
"""
import base64
import json
import sys
import time
import urllib.request

PROMPT = (
    "Преобразуй изображение страницы в markdown. Таблицу оформи как markdown-таблицу: сохрани все "
    "строки, колонки и значения, ничего не пропускай, числа не теряй и не переставляй. "
    "Отвечай только разметкой, без пояснений и без вступлений."
)


def main() -> int:
    path = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else "kag-qwen2vl:2b"
    with open(path, "rb") as f:
        img = base64.b64encode(f.read()).decode()
    payload = {
        "model": model,
        "prompt": PROMPT,
        "images": [img],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 4096},
    }
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        json.dumps(payload).encode(),
        {"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=1800).read())
    except Exception as e:  # noqa: BLE001
        print(f"  ошибка вызова: {type(e).__name__}: {str(e)[:200]}")
        return 1
    dt = time.time() - t0
    if data.get("error"):
        print(f"  Ollama вернула ошибку: {data['error']}")
        return 1
    md = data.get("response", "")
    table_lines = sum(1 for line in md.splitlines() if line.strip().startswith("|"))
    numbers = sum(ch.isdigit() for ch in md)
    print(f"  модель: {model}")
    print(f"  время: {dt:.1f} с | символов: {len(md)} | строк таблицы: {table_lines} | цифр в ответе: {numbers}")
    print("  --- начало ответа ---")
    print(md[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
