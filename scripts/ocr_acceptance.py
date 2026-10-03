"""Приёмка OCR-движков по арифметике строк таблицы (накладная).

Запускается В КОНТЕЙНЕРЕ api (или worker) на 18, где есть Occular и сетка, а до службы на 41 — сеть есть.
Самодостаточный: никаких импортов из 'scripts'.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, ".")



def run_occular(image: str) -> dict:
    from src.indexing.table_strategy import _raw_lines_from_occular

    data = pathlib.Path(image).read_bytes()
    import time
    t = time.time()
    lines = _raw_lines_from_occular(data)
    return {"engine": "occular", "seconds": round(time.time() - t, 1), "lines": lines, "image": image}


def acceptance(result: dict) -> dict:
    from src.indexing.table_strategy import recognize_grid_tables
    from src.indexing.table_validate import check_table

    lines = result.get("lines") or []
    image = result.get("image") or ""
    if not lines:
        return {"checked": 0, "ok": 0, "mismatch": 0, "verdict": "нет строк"}
    try:
        data = pathlib.Path(image).read_bytes()
        tables, reason = recognize_grid_tables(data, raw_lines=lines)
        if not tables:
            return {"checked": 0, "ok": 0, "mismatch": 0, "verdict": f"таблица не собрана: {reason}"}
        table = tables[0]
        verdict = check_table(table.rows, header_rows=1)
        return {"checked": verdict.checked, "ok": verdict.ok, "mismatch": verdict.mismatch,
                "verdict": verdict.verdict, "quality": getattr(table, "quality", 0.0), "reason": reason}
    except Exception as e:  # noqa: BLE001
        return {"checked": 0, "ok": 0, "mismatch": 0, "verdict": f"{type(e).__name__}: {str(e)[:140]}"}


def main() -> int:
    image = sys.argv[1] if len(sys.argv) > 1 else \
        "/app/data/uploads/9e116f5c-edee-4574-a854-5361be9be855_skan-nakladnoj3.png"
    print(f"изображение: {image}\n")
    for name, fn in (("occular", run_occular), ("service", run_service_ocr)):
        try:
            result = fn(image)
            score = acceptance(result)
            print(f"[движок] {name}: {result.get('seconds')} с, строк {len(result.get('lines') or [])}")
            print(f"  арифметика: проверено {score['checked']} | сошлось {score['ok']} | "
                  f"расхождений {score['mismatch']} | качество {score.get('quality')}")
            print(f"  вердикт: {score['verdict']}")
        except Exception as e:  # noqa: BLE001
            print(f"[движок] {name}: ОШИБКА {type(e).__name__}: {str(e)[:180]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())