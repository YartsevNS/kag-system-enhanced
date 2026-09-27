"""Проверка опции «таблицы без линий через VL-модель» на живом стенде.

Запуск внутри контейнера api (там есть и Occular, и клиент VL, и доступ к табличному слою):

    docker exec kag-api python /app/data/verify_tables_vlm.py --check
    docker exec kag-api python /app/data/verify_tables_vlm.py --check --image /app/data/uploads/<файл>.png
    docker exec kag-api python /app/data/verify_tables_vlm.py --enable | --disable
    docker exec kag-api python /app/data/verify_tables_vlm.py --document <id документа>

Что проверяет:
  1. `--check`   — состояние опции и реальная связь с сервисом моделей (та же функция, что у ручки админки);
  2. `--image`   — сквозной прогон схемы на конкретном файле: Occular, затем (если включено и он не нашёл)
                   VL-модель; печатает получившуюся таблицу — то, что уйдёт в табличный слой;
  3. `--enable`/`--disable` — включить/выключить опцию в настройках табличного слоя (по умолчанию выключена);
  4. `--document` — что лежит в табличном слое по документу: страницы, строки, качество, чем распознано.
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "/app")


def cmd_check(args) -> int:
    from src.indexing.vlm_tables import vlm_tables_status

    status = vlm_tables_status(check_service=True)
    print("=== опция и связь с сервисом моделей ===")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    if status.get("reachable"):
        print("\nвывод: сервис доступен" + (" и модель найдена" if status.get("model_present") else
                                            ", но модели в списке нет"))
    else:
        print("\nвывод: сервис НЕ доступен — таблицы без линий распознать не получится")

    if args.image:
        print(f"\n=== сквозной прогон на файле {args.image} ===")
        from src.indexing.table_strategy import recover_tables

        with open(args.image, "rb") as f:
            image = f.read()
        report = recover_tables(image)
        print(f"  путь: {report['technique']} | время: {report['seconds']} с")
        print(f"  причина: {report['reason']}")
        for i, table in enumerate(report["tables"], 1):
            print(f"  таблица {i}: {table.n_rows} строк × {table.n_cols} колонок "
                  f"(источник: {table.source}, модель: {table.source_model or '—'})")
            for row in table.rows[:12]:
                print("    | " + " | ".join((str(c)[:30] or "·") for c in row[:6]))
        if not report["tables"]:
            print("  таблиц не получилось — смотрите причину выше")
    return 0


def cmd_toggle(args) -> int:
    from src.api.services.config_store import config_store
    from src.indexing.vlm_tables import apply_settings_update, vlm_tables_status

    existing = config_store.get("tables", "config") or {}
    cfg = apply_settings_update(existing, {"enabled": bool(args.enable)})
    config_store.set("tables", "config", cfg)
    print(f"опция {'включена' if args.enable else 'выключена'}")
    print(json.dumps(vlm_tables_status(False), ensure_ascii=False, indent=2))
    return 0


def cmd_document(args) -> int:
    from sqlalchemy import text
    from src.database.session import get_session_local

    maker = get_session_local()
    with maker() as session:
        rows = session.execute(text(
            "select table_id, page_num, table_index, row_count, quality, model from document_tables "
            "where document_id = :d order by page_num, table_index"), {"d": args.document}).fetchall()
        doc = session.execute(text(
            "select left(id, 8), filename, file_type, chunks_count, status from documents where id = :d"),
            {"d": args.document}).fetchone()
    print(f"=== документ {args.document} ===")
    if doc:
        print(f"  {doc[1]} | тип {doc[2]} | фрагментов {doc[3]} | статус {doc[4]}")
    print(f"  таблиц в слое: {len(rows)}")
    for r in rows:
        print(f"    строк {r[3]:>3} | стр. {r[1]} | качество {r[4]} | чем распознано: {r[5]}")
    if not rows:
        print("  таблиц нет: либо опция была выключена, либо документ ещё не переобработан")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="проверить опцию и связь")
    ap.add_argument("--image", default="", help="файл для сквозного прогона схемы")
    ap.add_argument("--enable", action="store_true", help="включить опцию")
    ap.add_argument("--disable", action="store_true", help="выключить опцию")
    ap.add_argument("--document", default="", help="показать таблицы документа")
    args = ap.parse_args()

    if args.enable or args.disable:
        return cmd_toggle(args)
    if args.document:
        return cmd_document(args)
    return cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
