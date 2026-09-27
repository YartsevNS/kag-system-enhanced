"""Прототип 2: сетка по линиям + строки от полного конвейера Occular, разрезанные по линиям.

Почему так: низкоуровневый recognize(quads) работает без языковой модели и даёт уверенность 0,0–0,04
(проверено на скане накладной). Полный конвейер (детектор + распознаватель + языковая модель) читает ту же
страницу нормально. Значит правильный путь: взять строки с координатами от конвейера и РАЗРЕЗАТЬ каждую
строку по вертикальным линиям сетки, а по горизонтальным — определить строку таблицы. Тогда содержимое
попадает в свою ячейку и языковая модель работает.

Запуск:
    docker exec -i kag-api python /app/data/probe_grid_lines.py <картинка>
"""
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, "/app")

INSET = 2
SCALE = 3


def detect_grid(img_bgr):
    h, w = img_bgr.shape[:2]
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    bw = cv2.adaptiveThreshold(255 - gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)
    hor = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 30), 1)))
    ver = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, h // 40))))
    hproj, vproj = (hor > 0).sum(axis=1), (ver > 0).sum(axis=0)

    def group(idx, gap=4):
        out = []
        for i in idx:
            if out and i - out[-1][-1] <= gap:
                out[-1].append(i)
            else:
                out.append([i])
        return [int(round(sum(g) / len(g))) for g in out]

    vlines = group([j for j, v in enumerate(vproj) if v > h * 0.10])
    if len(vlines) < 3:
        return None
    y_min = min(int(np.argmax(ver[:, x] > 0)) for x in vlines)
    y_max = max(h - 1 - int(np.argmax(ver[::-1, x] > 0)) for x in vlines)
    x_min, x_max = min(vlines), max(vlines)
    hlines = group([y_min + i for i, v in enumerate(hproj[y_min:y_max + 1])
                    if v > (x_max - x_min) * 0.5])
    if len(hlines) < 3:
        return None
    # вертикальные линии, реально присутствующие в каждой полосе (даёт объединённые ячейки)
    present = []
    for ri in range(len(hlines) - 1):
        y0, y1 = hlines[ri] + INSET, hlines[ri + 1] - INSET
        row = [x for x in vlines if (ver[max(0, y0):y1, max(0, x - 1):x + 2] > 0).mean() > 0.5]
        present.append(sorted(set([x_min] + row + [x_max])))
    return {"hlines": hlines, "vlines": vlines, "row_lines": present,
            "bbox": (x_min, y_min, x_max, y_max)}


def cell_for(row_lines, x_center):
    """В какой интервал колонок попал центр фрагмента: (индекс, x0, x1)."""
    for i in range(len(row_lines) - 1):
        if row_lines[i] <= x_center < row_lines[i + 1]:
            return i, row_lines[i], row_lines[i + 1]
    return None, None, None


def main() -> int:
    path = sys.argv[1]
    img0 = cv2.imread(path)
    h0, w0 = img0.shape[:2]
    img = cv2.resize(img0, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    print(f"  файл: {path} | {w0}x{h0} -> x{SCALE} = {img.shape[1]}x{img.shape[0]}")

    t0 = time.time()
    grid = detect_grid(img)
    if not grid:
        print("  сетку по линиям построить не удалось")
        return 0
    print(f"  сетка: {len(grid['hlines'])} гор. × {len(grid['vlines'])} верт. линий за {time.time() - t0:.2f} с")

    from occular import ocr_detailed
    t1 = time.time()
    try:
        result = ocr_detailed(path, deskew=True, lm=True, orientation=False,
                              recognizer="svtr_lcnet")
    except TypeError:
        result = ocr_detailed(path)
    print(f"  полный конвейер: {time.time() - t1:.1f} с | тип результата {type(result).__name__}")

    # Разбираем результат на «текст + рамка»
    lines = []

    def walk(node, depth=0):
        if depth > 5 or node is None:
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item, depth + 1)
            return
        if isinstance(node, dict):
            text = node.get("text")
            box = node.get("bbox") or node.get("box") or node.get("quad") or node.get("points")
            if isinstance(text, str) and text.strip() and box is not None:
                b = np.asarray(box, dtype=float)
                if b.ndim == 2:
                    xs, ys = b[:, 0], b[:, 1]
                    box = [float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())]
                lines.append((text.strip(), [float(v) for v in np.asarray(box, dtype=float).ravel()[:4]],
                              float(node.get("confidence") or node.get("score") or 0.0)))
                return
            for value in node.values():
                walk(value, depth + 1)

    walk(result)
    print(f"  строк от конвейера: {len(lines)}")
    if not lines:
        print("  структура ответа не разобрана; пример:", str(result)[:300])
        return 0

    # Раскладываем: по вертикали — в свою полосу, по горизонтали — разрезаем по линиям колонок
    rows = {}
    for text, box, conf in lines:
        x0, y0, x1, y1 = box
        y_center = (y0 + y1) / 2
        ri = next((i for i in range(len(grid["hlines"]) - 1)
                   if grid["hlines"][i] <= y_center < grid["hlines"][i + 1]), None)
        if ri is None:
            continue
        row_lines = grid["row_lines"][ri]
        # какой колонке принадлежит ЗАПИСЬ: по её собственному центру
        ci, cx0, cx1 = cell_for(row_lines, (x0 + x1) / 2)
        if ci is None:
            continue
        rows.setdefault(ri, {})[ci] = (text, conf)

    total_cells = sum(len(v) for v in rows.values())
    print(f"  заполнено ячеек: {total_cells}")
    print("\n  === восстановленные строки (номер строки: содержимое колонок) ===")
    for ri in sorted(rows)[:12]:
        items = sorted(rows[ri].items())
        print(f"   строка {ri:>2}: " + " | ".join(f"[{ci}]{t[:24]}" for ci, (t, c) in items[:8]))

    confs = [c for v in rows.values() for _, c in v.values() if c]
    if confs:
        print(f"\n  средняя уверенность: {sum(confs) / len(confs):.2f} | ниже 0,5: "
              f"{sum(1 for c in confs if c < 0.5)} из {len(confs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
