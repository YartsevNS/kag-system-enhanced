"""Полная проба Occular: конвейер с порядком чтения + таблицы с раскладкой текста по ячейкам.

Отличие от первой пробы: правильные методы конвейера (`process_image`, `get_text`), полный дамп полей
ячейки (нужны координаты, чтобы положить в ячейку распознанный текст) и сама раскладка — итоговая
таблица «строка × колонка» с текстом и числами, как её увидит наш табличный слой.

Запуск на 41:
    ~/kag-eval/venv/bin/python occular_tables_fill.py <картинка>
"""
from __future__ import annotations

import sys
import time


def flatten(node, out, depth=0):
    """Собрать плоский список записей с текстом и координатами из любого вида результата."""
    if depth > 5 or node is None:
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            flatten(item, out, depth + 1)
        return
    if isinstance(node, dict):
        text = node.get("text")
        box = node.get("bbox") or node.get("box") or node.get("points")
        if isinstance(text, str) and box is not None:
            out.append(node)
            return
        for value in node.values():
            flatten(value, out, depth + 1)
        return
    text = getattr(node, "text", None)
    box = getattr(node, "bbox", None)
    if isinstance(text, str) and box is not None:
        out.append({"text": text, "bbox": box})


def box_of(item) -> tuple:
    box = item.get("bbox") if isinstance(item, dict) else None
    for key in ("bbox", "box", "points"):
        if isinstance(item, dict) and key in item:
            box = item[key]
            break
    if box is None:
        return None
    if isinstance(box, (list, tuple)) and box and isinstance(box[0], (list, tuple)):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        return (min(xs), min(ys), max(xs), max(ys))
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        return (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    return None


def fill_cells(table: dict, lines: list) -> list:
    """Разложить текст по ячейкам: ячейка берёт те строки OCR, чей центр попал в её рамку."""
    rows = table.get("rows") or []
    cols = table.get("cols") or []
    cells = table.get("cells") or []
    if not rows or not cols:
        return []

    grid = [["" for _ in cols] for _ in rows]

    def index_of(ranges, value):
        for i, rng in enumerate(ranges):
            lo, hi = float(rng[0]), float(rng[1])
            if lo <= value < hi:
                return i
        return None

    for line in lines:
        box = box_of(line)
        text = (line.get("text") if isinstance(line, dict) else "") or ""
        text = str(text).strip()
        if not box or not text:
            continue
        cx = (box[0] + box[2]) / 2.0
        cy = (box[1] + box[3]) / 2.0
        r, c = index_of(rows, cy), index_of(cols, cx)
        if r is None or c is None:
            continue
        grid[r][c] = f"{grid[r][c]} {text}".strip() if grid[r][c] else text
    return grid


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
        print(f"  версия occular: {getattr(occular, '__version__', '?')} | размер {img.shape[1]}x{img.shape[0]}")

        lines: list = []
        try:
            pipe = OCRPipeline(Settings(reading_order=True))
            t0 = time.time()
            result = pipe.process_image(path)
            print(f"  [конвейер + порядок чтения] время: {time.time() - t0:.1f} с | тип: {type(result).__name__}")
            print(f"  [конвейер] поля: {list(result.keys())[:10] if isinstance(result, dict) else '—'}")
            flatten(result, lines)
            print(f"  [конвейер] строк с координатами: {len(lines)}")
            if lines:
                sample = " / ".join(str(l.get('text', ''))[:25] for l in lines[:6])
                print(f"    первые: {sample}")
        except Exception as e:  # noqa: BLE001
            print(f"  [конвейер] ошибка: {type(e).__name__}: {str(e)[:200]}")

        try:
            tr = TableRecognizer()
            t0 = time.time()
            tables = tr(img)
            print(f"  [таблицы] время: {time.time() - t0:.1f} с | найдено: {len(tables)}")
            for i, t in enumerate(tables, 1):
                cells = t.get("cells") or []
                print(f"    таблица {i}: строк {len(t.get('rows') or [])} | колонок {len(t.get('cols') or [])} "
                      f"| ячеек {len(cells)}")
                if cells:
                    print(f"      поля ячейки: {sorted(cells[0].keys()) if isinstance(cells[0], dict) else type(cells[0])}")
                grid = fill_cells(t, lines)
                if grid:
                    filled = sum(1 for row in grid for c in row if c)
                    print(f"      заполнено ячеек текстом: {filled} из {len(grid) * len(grid[0])}")
                    for row in grid[:6]:
                        print("      | " + " | ".join((c[:26] or "·") for c in row[:6]))
        except Exception as e:  # noqa: BLE001
            print(f"  [таблицы] ошибка: {type(e).__name__}: {str(e)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
