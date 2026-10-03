"""Финальный прогон флоу v3: строки прямо из HTML RapidTable → OCR cyrillic по строке целиком → контрольные числа.

Не строю свои полосы: беру <tr> из HTML RapidTable. Для каждой строки bbox = min/max по её ячейкам
(но ячейки в HTML не привязаны к bbox напрямую; строку находим как y-диапазон центров ячеек, попадающих в неё).
Затем — одна вырезка на строку, OCR, контрольные числа.
"""
import re
import sys
import time
from html.parser import HTMLParser
import numpy as np
from PIL import Image

from rapid_table import RapidTable
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion


class T(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows = []; self.cur = []; self.in_td = False
    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.cur = []
        if tag in ('td', 'th'): self.in_td = True; self.cur.append('')
    def handle_data(self, d):
        if self.in_td: self.cur[-1] += d
    def handle_endtag(self, tag):
        if tag in ('td', 'th'): self.in_td = False
        if tag == 'tr' and self.cur: self.rows.append(self.cur)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else '/home/yartsevn/ocr-bakeoff/naklad.png'
    t0 = time.time()
    rt = RapidTable()
    out = rt(path)
    html = out.pred_htmls[0]
    bboxes = np.asarray(out.cell_bboxes[0], dtype=float).reshape(len(out.cell_bboxes[0]), -1, 2)
    print(f"[1] RapidTable {time.time()-t0:.1f} с, ячеек {len(bboxes)}")

    p = T(); p.feed(html)
    html_rows = p.rows   # 21 строка
    n_tr = len(html_rows)
    print(f"[2] HTML-строк: {n_tr}")

    # Для каждой <tr> находим диапазон y: [min(cell_y_center)-..., max(...)]. Точнее: строка = центр-полоса.
    # Сортцентры: свяжем ячейку со строкой, если её центр попадает в y-диапазон строки.
    # Диапазон строки оценим: y = min..max центров ячеек, попавших в <tr> — но связь неизвестна.
    # Проще: строки в bbox идут сверху вниз; число строк известно (n_tr). Разбиваем bbox по y на n_tr кластеров
    # равной медианной высоты (как строки сетки).
    centers_y = np.array([(b[:,1].min()+b[:,1].max())/2 for b in bboxes])
    order = np.argsort(centers_y)
    # медианная высота строки = медиана (max_y - min_y)
    heights = np.array([b[:,1].max()-b[:,1].min() for b in bboxes])
    h_med = float(np.median(heights)) if len(heights) else 10.0
    print(f"[3] медианная высота ячейки {h_med:.1f}")

    # кластеризуем: новая строка, когда разрыв центра > 1.3*h_med
    thr = 1.3 * h_med
    bands = []
    cur = [order[0]]
    for idx in order[1:]:
        if centers_y[idx] - centers_y[cur[-1]] <= thr:
            cur.append(idx)
        else:
            bands.append(cur); cur = [idx]
    if cur: bands.append(cur)
    print(f"[4] полос по центру+медиане: {len(bands)}")

    ocr = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5,
                           "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})
    img = Image.open(path).convert('RGB')
    arr = np.array(img)
    t0 = time.time()
    lines = []
    for band in bands:
        band_sorted = sorted(band, key=lambda i: bboxes[i][:,0].min())
        x0 = min(bboxes[i][:,0].min() for i in band_sorted)
        x1 = max(bboxes[i][:,0].max() for i in band_sorted)
        y0 = min(bboxes[i][:,1].min() for i in band_sorted)
        y1 = max(bboxes[i][:,1].max() for i in band_sorted)
        crop = arr[max(0,int(y0)):min(arr.shape[0],int(y1)), max(0,int(x0)):min(arr.shape[1],int(x1))]
        if crop.size == 0: continue
        res = ocr(crop)
        txt = str(res.txts[0]) if getattr(res,'txts',None) and res.txts else ''
        if txt.strip(): lines.append(txt)
    print(f"[5] OCR {time.time()-t0:.1f} с, строк с текстом {len(lines)}")
    for l in lines[:14]: print("   ", l[:130])

    all_text = ' '.join(lines)
    clean = re.sub(r'[^0-9,.]+', '', all_text).replace(',', '.')
    nums = ['13959.9','2791.98','16751.90','57834.75','45305.47','43250.36']
    found = [n for n in nums if n in clean]
    print(f"\n[6] контрольные числа: {len(found)}/{len(nums)} -> {found}")


if __name__ == '__main__':
    main()
