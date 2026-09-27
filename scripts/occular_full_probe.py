"""Полная проверка возможностей Occular на сервере моделей (41).

Ставим occular со всеми опциональными возможностями и смотрим, что он даёт на наших файлах:
  * распознавание текста (детектор + распознаватель + языковая модель);
  * определение ориентации страницы (0/90/180/270);
  * порядок чтения для многоколоночных страниц (модель layout);
  * TableRecognizer: сетка строк/колонок + объединённые ячейки (модель структуры требует torch);
  * раскладка распознанного текста по ячейкам таблицы — по координатам.

Запуск на 41:
    ~/kag-eval/venv/bin/python occular_full_probe.py <картинка> [...]
"""
from __future__ import annotations

import sys
import time


def _bbox(entry):
    """Достать координаты из записи OCR, как бы они ни назывались."""
    if isinstance(entry, dict):
        for key in ("bbox", "box", "points", "rect", "quad"):
            if key in entry:
                return entry[key]
        if "text" in entry:
            return None
    return getattr(entry, "bbox", None)


def _text(entry):
    if isinstance(entry, dict):
        return str(entry.get("text") or entry.get("value") or "")
    return str(getattr(entry, "text", "") or "")


def describe(obj, name: str, depth: int = 0) -> None:
    pad = "  " * (depth + 1)
    print(f"{pad}{name}: {type(obj).__name__}")
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:8]:
            print(f"{pad}  {k} = {str(v)[:110]}")
    elif isinstance(obj, (list, tuple)):
        print(f"{pad}  элементов: {len(obj)}")
        if obj:
            describe(obj[0], f"{name}[0]", depth + 1)


def flatten_boxes(entry, out, depth=0):
    """Собрать из результата OCR плоский список (текст, bbox), где бы он ни лежал."""
    if depth > 4:
        return
    if isinstance(entry, (list, tuple)):
        for e in entry:
            flatten_boxes(e, out, depth + 1)
        return
    text = _text(entry)
    box = _bbox(entry)
    if text and box is not None:
        out.append((text, box))
        return
    if isinstance(entry, dict):
        for v in entry.values():
            flatten_boxes(v, out, depth + 1)


def run(path: str) -> None:
    import cv2
    import occular

    print(f"\n=== файл: {path} ===")
    print(f"  версия occular: {getattr(occular, '__version__', '?')}")
    try:
        from occular import model_info
        print("  --- веса (model_info) ---")
        model_info()
    except Exception as e:  # noqa: BLE001
        print(f"  model_info недоступен: {e}")

    img = cv2.imread(path)
    if img is None:
        print("  не удалось открыть файл")
        return
    print(f"  размер: {img.shape[1]}x{img.shape[0]} px")

    # 1) Полный конвейер: ориентация и порядок чтения
    try:
        from occular import OCRPipeline, Settings
        settings = Settings(reading_order=True, orientation=True)
        pipe = OCRPipeline(settings)
        t0 = time.time()
        result = None
        for call in ("__call__", "run", "process", "recognize"):
            try:
                result = getattr(pipe, call)(path) if call != "__call__" else pipe(path)
                break
            except Exception:  # noqa: BLE001 — пробуем следующий способ вызова
                continue
        print(f"  [конвейер] время: {time.time() - t0:.1f} с")
        if result is None:
            print("  [конвейер] не удалось вызвать; доступные методы:",
                  [m for m in dir(pipe) if not m.startswith('_')][:20])
        else:
            describe(result, "результат", 1)
            boxes = []
            flatten_boxes(result, boxes)
            print(f"  [конвейер] распознанных единиц с координатами: {len(boxes)}")
            if boxes:
                print(f"    пример: {boxes[0][0][:60]!r} bbox={boxes[0][1]}")
    except Exception as e:  # noqa: BLE001
        print(f"  [конвейер] ошибка: {type(e).__name__}: {str(e)[:200]}")

    # 2) Простое OCR (для сравнения)
    try:
        from occular import ocr
        t0 = time.time()
        text = ocr(path)
        if isinstance(text, dict):
            text = text.get("text", "")
        print(f"  [просто OCR] время: {time.time() - t0:.1f} с | символов: {len(str(text))}")
    except Exception as e:  # noqa: BLE001
        print(f"  [просто OCR] ошибка: {type(e).__name__}: {str(e)[:160]}")

    # 3) Таблицы: сетка + ячейки
    try:
        from occular import TableRecognizer
        tr = TableRecognizer()
        t0 = time.time()
        tables = tr(img)
        print(f"  [таблицы] время: {time.time() - t0:.1f} с | найдено: {len(tables)}")
        for i, t in enumerate(tables, 1):
            rows = t.get("rows") or []
            cols = t.get("cols") or []
            cells = t.get("cells") or []
            print(f"    таблица {i}: строк {len(rows)} | колонок {len(cols)} | ячеек с данными {len(cells)}")
            print(f"      bbox: {t.get('bbox')}")
            if cells:
                describe(cells[0], "      первая ячейка", 3)
                texts = []
                for c in cells[:20]:
                    if isinstance(c, dict):
                        texts.append(str(c.get("text") or c.get("value") or "").strip()[:30])
                    else:
                        texts.append(str(c)[:30])
                print(f"      первые ячейки: {texts}")
    except Exception as e:  # noqa: BLE001
        print(f"  [таблицы] ошибка: {type(e).__name__}: {str(e)[:200]}")


def main() -> int:
    for path in sys.argv[1:]:
        try:
            run(path)
        except Exception as e:  # noqa: BLE001
            print(f"  файл {path}: ошибка {type(e).__name__}: {str(e)[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
