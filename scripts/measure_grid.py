"""Замер табличного пути на нескольких документах (проверка общности, а не подгонки под один файл).

Показывает по каждому файлу: применённый наклон, выбранное увеличение, размер сетки, заполненность,
качество и первые строки — то есть видно, работает ли схема на разных типах документов.

Запуск в контейнере api:
    docker exec -i kag-api python /app/data/measure_grid.py /app/data/a.png /app/data/b.png
"""
import sys
import time

import cv2
import numpy as np

from src.indexing.table_grid import fill_cells
from src.indexing.table_strategy import make_cell_recognizer, recognize_grid_tables


def report(path: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    name = path.rsplit("/", 1)[-1]
    started = time.time()
    tables, reason = recognize_grid_tables(data)
    elapsed = time.time() - started
    print(f"\n=== {name} ({len(data) / 1024:.0f} КБ)")
    print(f"    {reason} | {elapsed:.1f} с")
    if not tables:
        return
    table = tables[0]
    print(f"    заметки: {'; '.join(table.notes[:5]) or '—'}")
    filled = sum(1 for r in table.rows for c in r if str(c).strip())
    total = sum(len(r) for r in table.rows)
    print(f"    ячеек: {filled}/{total} ({100 * filled / max(1, total):.0f}%)")
    for i, row in enumerate(table.rows[:5]):
        cells = [str(c) for c in row if str(c).strip()]
        print(f"    строка {i + 1}: " + " | ".join(cells)[:160])


def main() -> int:
    for path in sys.argv[1:]:
        try:
            report(path)
        except Exception as e:  # noqa: BLE001 — замер не должен падать на одном файле
            print(f"\n=== {path.rsplit('/', 1)[-1]}: ошибка {type(e).__name__}: {str(e)[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
