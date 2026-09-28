"""Приёмка OCR-движков по арифметике строк таблицы (накладная).

Сравниваем два движка на одном изображении:
  * occular — текущий (32 с на страницу, 3/7 контрольных чисел);
  * service — PP-OCRv5 cyrillic через службу на 41 (3 с, 5/7).

Метрика — НЕ контрольные числа, а доля строк таблицы, проходящих арифметику
(количество × цена = стоимость), через ту же цепочку, что в проде
(recognize_grid_tables + table_validate). Планка текущего — 0 из 20.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.ocr_bakeoff import acceptance, run_occular, run_service_ocr  # noqa: E402

IMAGE = sys.argv[1] if len(sys.argv) > 1 else \
    "reports/_scratch/naklad.png"  # копия: /home/yartsevn/kag-system/data/uploads/…_skan-nakladnoj3.png


def main() -> int:
    print(f"изображение: {IMAGE}\n")
    for name, fn in (("occular", run_occular), ("service", run_service_ocr)):
        try:
            result = fn(IMAGE)
            result["image"] = IMAGE
            score = acceptance(result)
            print(f"[движок] {name}: {result.get('seconds')} с, строк {len(result.get('lines') or [])}")
            print(f"  арифметика: проверено {score['checked']} | сошлось {score['ok']} | "
                  f"расхождений {score['mismatch']} | качество {score.get('quality')}")
            print(f"  вердикт: {score['verdict']}")
        except Exception as e:  # noqa: BLE001
            print(f"[движок] {name}: ОШИБКА {type(e).__name__}: {str(e)[:160]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())