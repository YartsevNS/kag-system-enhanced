"""Замер OCR-службы на 41: скорость и качество на НАСТОЯЩИХ страницах корпуса.

Зачем отдельный прибор. «Служба отвечает /health» — это не проверка OCR: она ничего не говорит ни о
скорости, ни о том, что текст вменяемый. Прибор рендерит страницы реального PDF, отправляет их службе
так же, как это делает конвейер (`POST /ocr?lang=…` с PNG), и печатает время на страницу, число строк,
долю кириллицы и первые распознанные строки — по ним видно и «работает», и «работает правильно».

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/ocr_service_probe.py --pdf /app/data/inbox/<файл>.pdf --pages 1,2
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default="1")
    ap.add_argument("--langs", default="cyrillic,eslav")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    from src.api.services.config_store import config_store

    cfg = config_store.get("ocr", "settings") or {}
    url = str(cfg.get("service_url") or "http://192.168.50.41:8021").rstrip("/")
    print(f"служба: {url} (включена в настройках: {bool(cfg.get('service_enabled'))})")
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=5) as r:
            print(f"здоровье: {json.loads(r.read().decode())}")
    except Exception as e:  # noqa: BLE001
        print(f"недоступна: {type(e).__name__}: {e}")
        return 1

    import fitz  # PyMuPDF

    doc = fitz.open(args.pdf)
    pages = [int(p) - 1 for p in str(args.pages).split(",") if p.strip()]
    langs = [lang.strip() for lang in args.langs.split(",") if lang.strip()]
    print(f"файл: {args.pdf}; страниц в документе: {doc.page_count}; замер по страницам {args.pages}; "
          f"языки {langs}; DPI {args.dpi}\n")

    for lang in langs:
        for index in pages:
            if index >= doc.page_count:
                print(f"  страница {index + 1}: в документе нет")
                continue
            t0 = time.time()
            pix = doc[index].get_pixmap(dpi=args.dpi)
            png = pix.tobytes("png")
            render_s = time.time() - t0

            t1 = time.time()
            req = urllib.request.Request(f"{url}/ocr?lang={lang}", png, {"Content-Type": "image/png"})
            try:
                with urllib.request.urlopen(req, timeout=180) as response:
                    data = json.loads(response.read().decode())
            except Exception as e:  # noqa: BLE001
                print(f"  стр {index + 1} ({lang}): ошибка вызова {type(e).__name__}: {str(e)[:100]}")
                continue
            ocr_s = time.time() - t1

            lines = data.get("lines") or data.get("text") or []
            if isinstance(lines, str):
                lines = [lines]
            texts = [(l.get("text") if isinstance(l, dict) else str(l)) or "" for l in lines]
            joined = " ".join(texts)
            cyr = sum(1 for ch in joined if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
            letters = sum(1 for ch in joined if ch.isalpha())
            share = (cyr / letters * 100) if letters else 0.0
            print(f"  стр {index + 1:<3} {lang:<9} рендер {render_s:5.2f} с | OCR {ocr_s:6.2f} с | "
                  f"строк {len(texts):>4} | символов {len(joined):>6} | кириллица {share:5.1f}%")
            sample = [t for t in texts if t.strip()][:3]
            for s in sample:
                print(f"        {s[:110]}")
    doc.close()
    print("\nчтение: OCR-время — это чистое распознавание страницы службой; рендер делает клиент.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
