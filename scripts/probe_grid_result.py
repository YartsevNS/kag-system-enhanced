"""Проверка боевого пути на конкретном файле: печатает таблицу, которую восстановила система.

Запуск в контейнере api:
    docker exec -i kag-api python /app/data/probe_grid_result.py "/app/data/uploads/<файл>"
"""
import sys

sys.path.insert(0, "/app/data")   # свежие модули рядом со скриптом (важнее образа)
sys.path.insert(0, "/app")


def main() -> int:
    path = sys.argv[1]
    with open(path, "rb") as f:
        image = f.read()

    try:
        from table_strategy import recover_tables          # новая версия рядом со скриптом
    except Exception:
        from src.indexing.table_strategy import recover_tables

    report = recover_tables(image)
    print(f"  путь: {report['technique']} | время: {report['seconds']} с")
    print(f"  причина: {report['reason']}")
    for t in report["tables"]:
        rows = t.rows
        width = max(len(r) for r in rows) if rows else 0
        filled = sum(1 for r in rows for c in r if str(c).strip())
        print(f"\n  таблица: {len(rows)} строк × {width} колонок | заполнено {filled} ячеек "
              f"| качество {t.quality} | источник {t.source_model}")
        for i, r in enumerate(rows[:14]):
            print(f"   {i:>2}: " + " | ".join((str(c)[:22] if str(c).strip() else "·") for c in r[:9]))
    if not report["tables"]:
        print("  таблица не восстановлена")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
