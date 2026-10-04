"""Прототип: собрать PDF с невидимым текстовым слоем из нашего OCR (картинка + строки с координатами).

Зачем: у скана нет текстового слоя, поэтому текст нельзя выделить на самом документе. Вместо самодельного
слоя в просмотрщике можно собрать «настоящий» PDF с невидимым текстом по координатам распознавания — тогда
штатный просмотрщик (pdf.js) показывает страницу, выделяет и копирует текст, работает поиск по странице.

Запуск в контейнере api:
    docker exec kag-api python /app/data/proto_searchable_pdf.py <документ_id>
"""
import glob
import json
import sys
import time

import fitz

DOC = sys.argv[1] if len(sys.argv) > 1 else "e4da0d35-a70d-48a7-894a-368a93aa7054"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
OUT = "/tmp/searchable_test.pdf"


def main() -> int:
    pngs = glob.glob(f"/app/data/uploads/{DOC}_*")
    if not pngs:
        print("файл документа не найден")
        return 1
    png = pngs[0]
    lines_files = glob.glob(f"/app/data/ocr_results/{DOC}_*lines.json")
    if not lines_files:
        print("строк OCR с координатами нет (документ не переобработан)")
        return 1
    data = json.load(open(lines_files[0], encoding="utf-8"))
    width, height = data["width"], data["height"]
    lines = data["lines"]
    print(f"страница {width}x{height}, строк {len(lines)}")

    started = time.time()
    doc = fitz.open()
    # Страница в пунктах 1:1 с пикселями скана — тогда координаты строк ложатся как есть.
    page = doc.new_page(width=width, height=height)
    page.insert_image(fitz.Rect(0, 0, width, height), filename=png)
    page.insert_font(fontname="F0", fontfile=FONT)   # DejaVuSans: есть кириллица

    placed = 0
    for line in lines:
        text = (line.get("text") or "").strip()
        quad = line.get("quad") or []
        if not text or not quad:
            continue
        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        h = max(1.0, y1 - y0)
        # render_mode=3 — текст невидим (виден сам скан), но выделяется, копируется и ищется.
        page.insert_text((x0, y1 - h * 0.22), text, fontname="F0", fontsize=h * 0.78,
                         render_mode=3)
        placed += 1
    doc.save(OUT, deflate=True, garbage=3)
    doc.close()
    build_s = round(time.time() - started, 2)

    check = fitz.open(OUT)
    extracted = check[0].get_text("text")
    # Сверяем позиции: первая строка текста из PDF должна попасть туда же, где была на скане.
    first_rect = None
    for block in check[0].get_text("dict")["blocks"]:
        for ln in block.get("lines", []):
            first_rect = fitz.Rect(ln["bbox"])
            break
        if first_rect:
            break
    check.close()

    size_kb = round(len(open(OUT, "rb").read()) / 1024, 1)
    print(f"вставлено строк: {placed} | время сборки: {build_s} с | размер PDF: {size_kb} КБ")
    print(f"текст извлекается: {len(extracted)} символов")
    print("первые строки из PDF:", [s for s in extracted.splitlines() if s.strip()][:4])
    if first_rect:
        print(f"первая строка в PDF: x={first_rect.x0:.0f}..{first_rect.x1:.0f}, "
              f"y={first_rect.y0:.0f}..{first_rect.y1:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
