"""Сквозной прогон схемы на файле владельца: Occular → VL-модель (без изменения настроек в базе).

Опция включается только в памяти процесса: так тестируем сам механизм, не меняя поведение системы
(в базе она остаётся выключенной, пока владелец не решит иначе).

    docker exec kag-api python /app/data/e2e_vlm_table_test.py "/app/data/uploads/<файл>.png"
"""
import sys
import time

sys.path.insert(0, "/app")


def main() -> int:
    path = sys.argv[1]
    from src.indexing.table_strategy import recover_tables
    from src.indexing.vlm_tables import get_vlm_tables_config

    cfg = get_vlm_tables_config()
    cfg["enabled"] = True                       # только в памяти: в базе не меняем
    print(f"  файл: {path}")
    print(f"  настройки: {cfg['endpoint']} | модель {cfg['model']} | протокол {cfg['api']} | "
          f"таймаут {cfg['timeout_ms']} мс | предел {cfg['num_predict']} токенов")

    with open(path, "rb") as f:
        image = f.read()

    started = time.time()
    report = recover_tables(image, config=cfg)
    print(f"\n  итог: путь «{report['technique']}» | всего {time.time() - started:.1f} с")
    print(f"  причина: {report['reason']}")
    for i, table in enumerate(report["tables"], 1):
        print(f"\n  таблица {i}: {table.n_rows} строк × {table.n_cols} колонок | источник {table.source} | "
              f"модель {table.source_model or '—'} | {table.seconds} с")
        for note in table.notes:
            print(f"    замечание: {note}")
        for row in table.rows:
            print("    | " + " | ".join((str(c)[:38] or "·") for c in row[:5]))
    if not report["tables"]:
        print("  таблицу получить не удалось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
