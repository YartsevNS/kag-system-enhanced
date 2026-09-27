"""Сравнение двух путей восстановления таблиц на одном файле: свой Occular против модели зрения.

Зачем: на скане накладной (skan-nakladnoj3.png) Occular вернул сетку 15×29 с перемешанными ячейками,
и, поскольку таблица «нашлась», модель зрения не вызывалась вовсе. Нужен честный замер на одном файле:
что даёт каждый путь и есть ли смысл в проверке качества перед выбором.

Запуск внутри контейнера api:
    docker exec -i kag-api python /app/data/probe_vl_vs_occular.py /app/data/uploads/<файл>
"""
import sys

sys.path.insert(0, "/app")


def stats(rows):
    """Простая метрика пригодности таблицы: заполненность и «числовая насыщенность»."""
    if not rows:
        return {"rows": 0, "cols": 0, "filled": 0.0, "numeric": 0.0}
    width = max(len(r) for r in rows)
    total = len(rows) * width
    filled = sum(1 for r in rows for c in r if str(c).strip())
    numeric = 0
    for r in rows:
        for c in r:
            s = str(c).strip().replace(" ", "")
            digits = sum(ch.isdigit() for ch in s)
            if s and digits / max(1, len(s)) > 0.6:
                numeric += 1
    return {"rows": len(rows), "cols": width,
            "filled": round(filled / max(1, total), 3),
            "numeric": round(numeric / max(1, total), 3)}


def show(name, rows, extra=""):
    print(f"\n=== {name} ===")
    if not rows:
        print("  таблица не получена")
    else:
        st = stats(rows)
        print(f"  {st['rows']} строк × {st['cols']} колонок | заполнено {st['filled']:.0%} | "
              f"числовых ячеек {st['numeric']:.0%} {extra}")
        for r in rows[:6]:
            print("   ", " | ".join(str(c)[:22] for c in r[:8]))
    return rows


def main() -> int:
    path = sys.argv[1]
    with open(path, "rb") as f:
        image = f.read()

    # 1) Путь Occular (как сейчас в конвейере)
    from src.indexing.table_strategy import recognize_occular_tables, _lines_from_occular

    lines = _lines_from_occular(image)
    print(f"  строк OCR с координатами: {len(lines)}")
    occ_tables, occ_reason = recognize_occular_tables(image, ocr_lines=lines)
    print(f"  Occular: {occ_reason}")

    # 2) Путь модели зрения (принудительно)
    from src.indexing.vlm_tables import get_vlm_tables_config, recognize_table

    cfg = get_vlm_tables_config()
    cfg["enabled"] = True
    table, reason = recognize_table(image, page=1, config=cfg)
    print(f"  модель зрения: {reason}")

    occ_rows = occ_tables[0].rows if occ_tables else []
    show("Occular (сетка + раскладка строк)", occ_rows, f"| {len(occ_tables)} таблиц(ы)")
    show("Модель зрения (JSON-формат)", table.rows if table else [], f"| {table.source_model if table else '—'}")

    print("\n=== вывод ===")
    so, sv = stats(occ_rows), stats(table.rows if table else [])
    print(f"  Occular:       {so['rows']}×{so['cols']}, заполнено {so['filled']:.0%}, чисел {so['numeric']:.0%}")
    print(f"  Модель зрения: {sv['rows']}×{sv['cols']}, заполнено {sv['filled']:.0%}, чисел {sv['numeric']:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
