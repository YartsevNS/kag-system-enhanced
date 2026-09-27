"""Сравнение форматов ответа модели зрения для таблиц: markdown, HTML, JSON.

Зачем: владелец предложил просить у модели HTML — мол, таблицы будут лучше. Идея верная по сути
(только HTML умеет объединённые ячейки: colspan/rowspan, а markdown их не выражает вообще), но у неё
есть цена: HTML многословнее, а модель на CPU генерирует по токенам, то есть медленнее и рискованнее
(можно получить обрезанные или несбалансированные теги). JSON — третий вариант: компактнее HTML и
разбирается без парсера.

Этот замер отвечает цифрами: сколько времени, получилось ли разобрать ответ и сколько чисел столбца
«Кол» совпало с тем, что видно на картинке.

Запуск на сервере моделей (41):
    ~/kag-eval/venv/bin/python vlm_format_compare.py <картинка> [модель]
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request
from html.parser import HTMLParser

# Значения столбца «Кол» из файла владельца — эталон для сравнения (прочитаны по изображению).
REFERENCE = ["4", "28", "2", "43", "4", "12", "7", "4", "1", "4"]

PROMPTS = {
    "markdown": (
        "Преобразуй изображение в markdown-таблицу. Правила: только таблица, без вступлений; одна строка "
        "таблицы на строку изображения; повторять строки запрещено; числа сохраняй точно."
    ),
    "html": (
        "Преобразуй изображение в HTML-таблицу. Правила: только разметка <table>, без пояснений; "
        "заголовки в <thead><th>, данные в <tbody><td>; используй rowspan/colspan, если ячейки объединены; "
        "числа сохраняй точно."
    ),
    "json": (
        "Преобразуй изображение в JSON-массив строк таблицы. Правила: только JSON, без пояснений и без "
        "markdown-обёртки; формат [[\"заголовок1\",\"заголовок2\"],[\"значение1\",\"значение2\"]]; "
        "первая строка — заголовки; числа как строки, точно как на изображении."
    ),
}


class _TableHTMLParser(HTMLParser):
    """Достаёт строки и ячейки из HTML-ответа модели (без внешних зависимостей)."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self._in_cell = True
            self._cell = []
        elif tag == "tr":
            self._row = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._row.append(" ".join("".join(self._cell).split()))
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
            self._row = []

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data)


def ask(image_b64: str, model: str, prompt: str) -> tuple[str, float]:
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": 0, "top_k": 1, "num_predict": 2048},
        "stop": ["\n\n", "```"],
    }
    req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                 json.dumps(payload).encode(), {"Content-Type": "application/json"})
    started = time.time()
    data = json.loads(urllib.request.urlopen(req, timeout=1800).read())
    return data.get("response", ""), time.time() - started


def parse_markdown(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("|") and line.count("|") >= 2:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and all(set(c) <= {"-", ":", " "} and c for c in cells):
                continue
            rows.append(cells)
    return rows


def parse_html(text: str) -> list[list[str]]:
    parser = _TableHTMLParser()
    try:
        parser.feed(text)
    except Exception:  # noqa: BLE001 — модель могла прислать битую разметку
        pass
    return [r for r in parser.rows if any(c for c in r)]


def parse_json(text: str) -> list[list[str]]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("["):]
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        data = json.loads(cleaned[start:end + 1])
    except Exception:  # noqa: BLE001
        return []
    return [[str(c) for c in row] for row in data if isinstance(row, list)]


PARSERS = {"markdown": parse_markdown, "html": parse_html, "json": parse_json}


def numbers_of(rows: list[list[str]]) -> list[str]:
    """Все числа из таблицы по порядку — так сравним с эталоном независимо от числа колонок."""
    out = []
    for row in rows:
        for cell in row:
            cleaned = cell.strip().replace("\u00a0", " ")
            if cleaned.isdigit() and len(cleaned) <= 3:
                out.append(cleaned)
    return out


def main() -> int:
    path = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else "kag-qwen2vl:2b"
    with open(path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    print(f"  файл: {path} | модель: {model}")
    print(f"  эталон чисел столбца «Кол»: {', '.join(REFERENCE)}\n")
    results = {}
    for fmt, prompt in PROMPTS.items():
        text, seconds = ask(image_b64, model, prompt)
        rows = PARSERS[fmt](text)
        nums = numbers_of(rows)
        hit = sum(1 for r in REFERENCE if r in nums)
        results[fmt] = {"seconds": seconds, "chars": len(text), "rows": len(rows),
                        "parsed": bool(rows), "numbers": nums, "hit": hit}
        print(f"=== {fmt} ===")
        print(f"  время: {seconds:.1f} с | символов: {len(text)} | распознано строк: {len(rows)} "
              f"| разбор: {'да' if rows else 'НЕТ'}")
        print(f"  числа найдены ({len(nums)}): {', '.join(nums[:14])}")
        print(f"  совпало с эталоном: {hit} из {len(REFERENCE)}")
        if rows:
            for row in rows[:4]:
                print("    | " + " | ".join((c[:26] or "·") for c in row[:5]))
        print()

    print("=== ИТОГ ===")
    print(f"  {'формат':<10} {'время, с':>9} {'символов':>9} {'строк':>6} {'чисел':>6} {'совпало':>8}")
    for fmt, r in results.items():
        print(f"  {fmt:<10} {r['seconds']:>9.1f} {r['chars']:>9} {r['rows']:>6} "
              f"{len(r['numbers']):>6} {r['hit']:>4} из {len(REFERENCE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
