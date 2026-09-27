"""Occular: конвейер → строки с координатами → раскладка по ячейкам таблицы (итоговая таблица).

Запуск на 41:
    ~/kag-eval/venv/bin/python occular_tables_fill2.py <картинка>

Что делает по шагам:
  1. показывает структуру результата конвейера (какие поля у строки OCR) — чтобы не угадывать имена;
  2. достаёт из результата плоский список «текст + координаты», пробуя известные варианты имён;
  3. берёт от TableRecognizer сетку (диапазоны строк и колонок) и раскладывает по ней текст;
  4. печатает заполненную таблицу — то, что уйдёт в наш табличный слой.
"""
from __future__ import annotations

import sys
import time

TEXT_KEYS = ("text", "value", "content", "line", "label")
BOX_KEYS = ("bbox", "box", "points", "quad", "rect", "polygon")


def show_structure(node, name="результат", depth=0, max_depth=3):
    pad = "  " * (depth + 1)
    if depth > max_depth:
        return
    if isinstance(node, list):
        print(f"{pad}{name}: список, элементов {len(node)}")
        if node:
            show_structure(node[0], f"{name}[0]", depth + 1, max_depth)
        return
    if isinstance(node, dict):
        print(f"{pad}{name}: dict, поля {sorted(node.keys())[:12]}")
        for key in ("text", "bbox", "box", "points", "confidence", "score", "lines"):
            if key in node:
                print(f"{pad}  {key} = {str(node[key])[:110]}")
        return
    attrs = [a for a in dir(node) if not a.startswith("_")]
    print(f"{pad}{name}: {type(node).__name__}, атрибуты {attrs[:12]}")


def norm_box(box):
    if box is None:
        return None
    if isinstance(box, (list, tuple)):
        if box and isinstance(box[0], (list, tuple)) and len(box[0]) >= 2:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            return (min(xs), min(ys), max(xs), max(ys))
        if len(box) == 4:
            a, b, c, d = (float(v) for v in box)
            # [x, y, w, h] против [x0, y0, x1, y1]: если третья меньше первой — это ширина/высота
            if c < a or d < b:
                return (a, b, a + c, b + d)
            return (a, b, c, d)
    return None


def extract_lines(node, out, depth=0):
    if depth > 6 or len(out) > 4000:
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            extract_lines(item, out, depth + 1)
        return
    if isinstance(node, dict):
        text = next((node[k] for k in TEXT_KEYS if isinstance(node.get(k), str) and node[k].strip()), None)
        box = next((node[k] for k in BOX_KEYS if k in node), None)
        if text and box is not None:
            nb = norm_box(box)
            if nb:
                out.append({"text": text, "bbox": nb})
                return
        for value in node.values():
            extract_lines(value, out, depth + 1)
        return
    text = next((getattr(node, k) for k in TEXT_KEYS if isinstance(getattr(node, k, None), str)
                 and getattr(node, k).strip()), None)
    box = next((getattr(node, k) for k in BOX_KEYS if getattr(node, k, None) is not None), None)
    if text and box is not None:
        nb = norm_box(box)
        if nb:
            out.append({"text": text, "bbox": nb})


def fill(grid_rows, grid_cols, lines):
    def idx(ranges, value):
        for i, rng in enumerate(ranges):
            if float(rng[0]) <= value < float(rng[1]):
                return i
        return None

    grid = [["" for _ in grid_cols] for _ in grid_rows]
    used = 0
    for line in lines:
        x0, y0, x1, y1 = line["bbox"]
        r = idx(grid_rows, (y0 + y1) / 2)
        c = idx(grid_cols, (x0 + x1) / 2)
        if r is None or c is None:
            continue
        text = line["text"].strip()
        grid[r][c] = f"{grid[r][c]} {text}".strip() if grid[r][c] else text
        used += 1
    return grid, used


def main() -> int:
    import cv2
    import occular
    from occular import OCRPipeline, Settings, TableRecognizer

    for path in sys.argv[1:]:
        print(f"\n=== {path} ===")
        img = cv2.imread(path)
        if img is None:
            print("  файл не открылся")
            continue
        print(f"  occular {getattr(occular, '__version__', '?')} | {img.shape[1]}x{img.shape[0]} px")

        lines: list = []
        try:
            pipe = OCRPipeline(Settings(reading_order=True))
            t0 = time.time()
            result = pipe.process_image(path)
            print(f"  [конвейер, порядок чтения] {time.time() - t0:.1f} с")
            show_structure(result)
            extract_lines(result, lines)
            print(f"  строк OCR с координатами: {len(lines)}")
            for line in lines[:5]:
                print(f"    {line['text'][:48]!r} bbox={tuple(round(v, 1) for v in line['bbox'])}")
        except Exception as e:  # noqa: BLE001
            print(f"  [конвейер] ошибка: {type(e).__name__}: {str(e)[:180]}")

        try:
            tr = TableRecognizer()
            t0 = time.time()
            tables = tr(img)
            print(f"  [таблицы] {time.time() - t0:.1f} с | найдено {len(tables)}")
            for i, t in enumerate(tables, 1):
                rows, cols = t.get("rows") or [], t.get("cols") or []
                print(f"    таблица {i}: {len(rows)}x{len(cols)} | ячеек {len(t.get('cells') or [])}")
                if not (rows and cols) or not lines:
                    continue
                grid, used = fill(rows, cols, lines)
                nonempty = sum(1 for row in grid for c in row if c)
                print(f"      строк OCR попало в сетку: {used} | заполнено ячеек: {nonempty} "
                      f"из {len(rows) * len(cols)}")
                for row in grid[:8]:
                    print("      | " + " | ".join((c[:24] or "·") for c in row[:7]))
        except Exception as e:  # noqa: BLE001
            print(f"  [таблицы] ошибка: {type(e).__name__}: {str(e)[:180]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
