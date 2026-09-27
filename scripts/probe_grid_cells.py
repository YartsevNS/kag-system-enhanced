"""Прототип: своя сетка таблицы по линиям + распознавание каждой ячейки (проверка схемы).

Идея (владелец одобрил 1+2): сетку таблицы строим сами по линиям бланка — детерминированно и быстро,
а текст достаём распознаванием КАЖДОЙ ячейки отдельно: рамка ячейки передаётся распознавателю Occular
одним пакетным вызовом (сигнатура recognize(image, quads) → список пар «текст, уверенность»).

Почему так: библиотечный TableRecognizer на плотном скане накладной отдал 23 строки нулевой высоты из 30,
а текст в ячейки не кладёт вовсе. Линии на бланке видны, значит границы ячеек можно получить точно.

Запуск внутри контейнера api:
    docker exec -i kag-api python /app/data/probe_grid_cells.py <картинка>
"""
import json
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, "/app")

INSET = 3          # отступ от линий, чтобы сами линии не попали в распознавание
MIN_CELL_PX = 8    # ячейки уже этого не рассматриваем
SCALE = 3          # увеличение перед обработкой: у мелкого скана ячейки ~13 px, распознавателю нужно ~32 px


def detect_lines(gray: np.ndarray):
    """Горизонтальные и вертикальные линии бланка (морфология по бинаризованному изображению)."""
    h, w = gray.shape
    bw = cv2.adaptiveThreshold(255 - gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)
    hor = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                           cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 30), 1)))
    ver = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                           cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, h // 40))))
    hproj = (hor > 0).sum(axis=1)
    vproj = (ver > 0).sum(axis=0)
    return hor, ver, hproj, vproj


def group(idx, gap=4):
    out = []
    for i in idx:
        if out and i - out[-1][-1] <= gap:
            out[-1].append(i)
        else:
            out.append([i])
    return [int(round(sum(g) / len(g))) for g in out]


def main() -> int:
    path = sys.argv[1]
    img0 = cv2.imread(path)
    h0, w0 = img0.shape[:2]
    # Увеличение ДО распознавания: у низкоразрешённых сканов текст в ячейке ~8 px высотой,
    # и распознаватель на таком размере даёт мусор (уверенность 0,0). После увеличения — нормальный текст.
    img = cv2.resize(img0, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    print(f"  файл: {path} | исходно {w0}x{h0} | обрабатываю в масштабе x{SCALE} -> {w}x{h}")

    t0 = time.time()
    hor, ver, hproj, vproj = detect_lines(gray)
    vlines = group([j for j, v in enumerate(vproj) if v > h * 0.10])
    if len(vlines) < 3:
        print("  вертикальных линий мало — это не таблица с сеткой")
        return 0
    # Область таблицы: по вертикальным линиям берём их протяжённость, по ней — горизонтальные линии
    y_min = min(np.argmax(ver[:, x] > 0) for x in vlines)
    y_max = max(h - 1 - np.argmax(ver[::-1, x] > 0) for x in vlines)
    x_min, x_max = min(vlines), max(vlines)
    band = hproj[y_min:y_max + 1]
    hlines = [y_min + i for i, v in enumerate(band) if v > (x_max - x_min) * 0.5]
    hlines = group(hlines)
    print(f"  таблица: x {x_min}..{x_max}, y {y_min}..{y_max} | линии: {len(hlines)} гор. × {len(vlines)} верт. "
          f"| поиск линий {time.time() - t0:.2f} с")
    if len(hlines) < 3:
        print("  горизонтальных линий мало — таблицу не собрать")
        return 0

    # Ячейки: в каждой полосе (строке) учитываем только те вертикальные линии, которые в ней реально есть.
    # Это автоматически даёт объединённые ячейки: где линии нет — ячейка шире.
    quads, mapping = [], []
    for ri in range(len(hlines) - 1):
        y0, y1 = hlines[ri] + INSET, hlines[ri + 1] - INSET
        if y1 - y0 < MIN_CELL_PX:
            continue
        present = [x for x in vlines if (ver[y0:y1, max(0, x - 1):x + 2] > 0).mean() > 0.6]
        if not present:
            present = [x_min, x_max]
        present = sorted(set([x_min] + present + [x_max]))
        for ci in range(len(present) - 1):
            x0, x1 = present[ci] + INSET, present[ci + 1] - INSET
            if x1 - x0 < MIN_CELL_PX:
                continue
            quad = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32)
            quads.append(quad)
            mapping.append((ri, present[ci], present[ci + 1]))
    print(f"  ячеек к распознаванию: {len(quads)}")

    from occular import CRNNRecognizerONNX
    rec = CRNNRecognizerONNX(num_threads=4)
    t1 = time.time()
    results = rec.recognize(img, quads)
    print(f"  распознавание всех ячеек: {time.time() - t1:.1f} с")

    # Собираем строки таблицы
    table = {}
    filled = 0
    for (ri, x0, x1), (text, conf) in zip(mapping, results):
        text = (text or "").strip()
        table.setdefault(ri, []).append((x0, text, conf))
        if text:
            filled += 1
    print(f"  заполнено ячеек: {filled} из {len(quads)} ({filled / max(1, len(quads)):.0%})")
    print("\n  === первые строки собранной таблицы ===")
    for ri in sorted(table)[:10]:
        cells = [(x, t) for x, t, c in sorted(table[ri])]
        line = " | ".join((t[:26] if t else "·") for _, t in cells[:10])
        print(f"   строка {ri:>2}: {line}")

    print("\n  === примеры уверенности ===")
    pairs = [(t, c) for t, c in results if t][:8]
    for t, c in pairs:
        print(f"    {c:.2f}  {t[:50]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
