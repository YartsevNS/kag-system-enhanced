"""Калибровка признака «таблица прозаическая» для таблиц из ТЕКСТОВОГО слоя (без координат).

Зачем: у PDF с текстовым слоем таблицу сейчас даёт find_tables, и на прозаической таблице он рубит
абзацы в строки (конспект: 38 строк при истинных 5). Чтобы вовремя переключиться на детектор ячеек,
нужен признак, работающий по САМИМ строкам таблицы, без координат.

Меряем на трёх известных документах: длина ячейки (медиана), число строк и колонок, доля длинных ячеек.
"""
from pathlib import Path

import fitz

PAGES = [
    ("конспект 13611481-3 (проза, истина 5x3)", "/home/yartsevn/table-bakeoff/img/13611481-3.pdf"),
    ("смета Договор 09.11.2020 (числовая 8x3)", "/home/yartsevn/table-bakeoff/img/smeta.pdf"),
    ("HumynLabs 13611481-4 (проза)", "/home/yartsevn/table-bakeoff/img/13611481-4.pdf"),
    ("HumynLabs 13611481-5 (проза)", "/home/yartsevn/table-bakeoff/img/13611481-5.pdf"),
    ("HumynLabs 13611481-1", "/home/yartsevn/table-bakeoff/img/13611481-1.pdf"),
    ("HumynLabs 13611481-2", "/home/yartsevn/table-bakeoff/img/13611481-2.pdf"),
]


def stats(data: list) -> dict:
    cells = [str(c or "").strip() for row in data for c in row]
    cells = [c for c in cells if c]
    if not cells:
        return {}
    lens = sorted(len(c) for c in cells)
    med = lens[len(lens) // 2]
    long_share = sum(1 for c in cells if len(c) > 25) / len(cells)
    numeric = sum(1 for c in cells if c.replace(" ", "").replace(",", ".").replace("-", "").isdigit())
    return {"rows": len(data), "cols": max((len(r) for r in data), default=0),
            "median_len": med, "long_share": round(long_share, 2),
            "numeric_share": round(numeric / len(cells), 2)}


def main() -> None:
    for label, path in PAGES:
        p = Path(path)
        if not p.exists():
            print(f"{label}: файла нет")
            continue
        try:
            doc = fitz.open(str(p))
            tabs = doc[0].find_tables().tables
        except Exception as e:  # noqa: BLE001
            print(f"{label}: find_tables не сработал ({type(e).__name__})")
            continue
        if not tabs:
            print(f"{label}: find_tables таблиц не нашёл")
            continue
        for i, tb in enumerate(tabs, 1):
            data = tb.extract()
            s = stats(data)
            if not s:
                continue
            prose = s["median_len"] > 25 and s["long_share"] >= 0.5
            print(f"{label} | таблица {i}: строк {s['rows']:3} | колонок {s['cols']:2} | "
                  f"медиана длины ячейки {s['median_len']:3} | доля длинных {s['long_share']:.2f} | "
                  f"доля чисел {s['numeric_share']:.2f} | признак: {'ПРОЗА' if prose else 'числовая/короткая'}")


if __name__ == "__main__":
    main()
