"""Qwen2-VL (Ollama) для таблиц: ограничения генерации и чистка повторов.

Проблема, которую решаем: в первом замере модель выдала 91 строку таблицы вместо ~14 — она зациклилась
и повторяла одни и те же строки. Причины две: слишком большой лимит генерации и отсутствие штрафа за
повторы. Здесь: строгий промпт, ограничение длины, штраф за повторы, стоп-последовательности и
постобработка (выбрасываем подряд идущие дубли строк таблицы).

Запуск на 41:
    ~/kag-eval/venv/bin/python vlm_table_clean.py <картинка> [модель]
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request

PROMPT = (
    "Преобразуй изображение в markdown-таблицу. Правила: только таблица, без вступлений и пояснений; "
    "одна строка таблицы на одну строку изображения; повторять строки запрещено; если данные кончились — "
    "заверши ответ. Числа сохраняй точно, ничего не додумывай."
)


def call(path: str, model: str, num_predict: int, repeat_penalty: float) -> tuple[str, float, dict]:
    with open(path, "rb") as f:
        img = base64.b64encode(f.read()).decode()
    payload = {
        "model": model,
        "prompt": PROMPT,
        "images": [img],
        "stream": False,
        "options": {
            "temperature": 0,
            "top_k": 1,
            "num_predict": num_predict,
            "repeat_penalty": repeat_penalty,
            "repeat_last_n": 512,
        },
        "stop": ["\n\n", "Примечание:", "```"],
    }
    req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                 json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    t0 = time.time()
    data = json.loads(urllib.request.urlopen(req, timeout=1800).read())
    return data.get("response", ""), time.time() - t0, data


def markdown_rows(md: str) -> list:
    rows = []
    for line in md.splitlines():
        line = line.strip()
        if line.startswith("|") and line.count("|") >= 2:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and all(set(c) <= {"-", ":", " "} and c for c in cells):
                continue
            rows.append(cells)
    return rows


def dedupe(rows: list) -> tuple[list, int]:
    """Убрать повторы: и подряд идущие, и полные дубликаты уже встречавшихся строк (кроме шапки)."""
    out, seen, dropped = [], set(), 0
    header = tuple(rows[0]) if rows else None
    for i, row in enumerate(rows):
        key = tuple(row)
        if i > 0 and (key in seen):
            dropped += 1
            continue
        if i > 0:
            seen.add(key)
        out.append(row)
    return out, dropped


def main() -> int:
    path = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else "kag-qwen2vl:2b"
    for num_predict, penalty in ((2048, 1.0), (2048, 1.05)):
        md, dt, data = call(path, model, num_predict, penalty)
        if data.get("error"):
            print(f"  ошибка Ollama: {data['error']}")
            return 1
        rows = markdown_rows(md)
        clean, dropped = dedupe(rows)
        print(f"\n=== {model} | num_predict={num_predict} повтор_штраф={penalty} ===")
        print(f"  время: {dt:.1f} с | символов: {len(md)} | строк таблицы: {len(rows)} "
              f"| после чистки: {len(clean)} (выброшено повторов: {dropped})")
        width = max((len(r) for r in clean), default=0)
        for row in clean[:20]:
            row = row + [""] * (width - len(row))
            print("  | " + " | ".join(c[:30] for c in row))
        if len(clean) > 20:
            print(f"  … ещё {len(clean) - 20} строк")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
