"""Разрешение Bronze в объекты и очередь разбора (Silver-шаг, без вызовов модели).

    python scripts/resolve_bronze.py --document fb4d9a84-e723-41fa-970d-2ed75683674d
    python scripts/resolve_bronze.py --run f16b25c3 --persist
    python scripts/resolve_bronze.py --document <id> --json

Без --persist расчёт только показывает числа; с --persist спорные строки попадают
в очередь разбора `bronze_queue` (идемпотентно, ничего не удаляется).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indexing import bronze, bronze_resolve  # noqa: E402


def _print_human(res: dict) -> None:
    print(f"прогон: {res['run_id']}")
    print(f"строк сущностей: {res['строк_сущностей']} → объектов: {res['объектов']} "
          f"(сжатие {res['сжатие']}×)")
    print("\nпо типам:")
    print(f"   {'тип':28} {'строк':>7} {'объектов':>9}")
    for t, d in res["по_типам"].items():
        print(f"   {t:28} {d['строк']:>7} {d['объектов']:>9}")
    req = res["требования"]
    print(f"\nтребования: групп по тексту {req['групп_по_тексту']}, "
          f"слито групп {req['слито_групп']}, разделено (разные id) {req['разделено_групп']}")
    print(f"   виды requirement-id: {req['по_видам_id']}")
    print(f"   с кодом меры: {req['с_кодом_меры']}")
    print(f"\nмеры с конфликтом названий: {res['меры_с_конфликтом']}")
    print(f"регуляторы по кодам: {res['регуляторы']}")
    print(f"\nочередь разбора: всего {res['очередь_всего']}")
    for reason, n in sorted(res["очередь"].items(), key=lambda kv: -kv[1]):
        print(f"   {reason:26} {n:>5}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Разрешение Bronze: объекты и очередь разбора")
    ap.add_argument("--run", help="run_id или его начало")
    ap.add_argument("--document", help="document_id: взять последний прогон документа")
    ap.add_argument("--persist", action="store_true", help="записать очередь разбора в БД")
    ap.add_argument("--json", action="store_true", help="вывести JSON")
    a = ap.parse_args()

    run_id = (a.run or "").strip()
    if run_id and len(run_id) < 32:
        resolved = bronze.run_by_prefix(run_id)
        if not resolved:
            print(f"прогон с началом «{run_id}» не найден", file=sys.stderr)
            return 1
        run_id = resolved
    if not run_id and a.document:
        runs = bronze.runs(a.document, 1)
        if not runs:
            print(f"прогонов Bronze для документа {a.document} нет", file=sys.stderr)
            return 1
        run_id = runs[0]["run_id"]

    if not run_id:
        print("нужен --run или --document", file=sys.stderr)
        return 2

    res = bronze_resolve.resolve(run_id, persist=a.persist)
    res.pop("объекты", None)
    res.pop("очередь_строки", None)

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _print_human(res)
        if not a.persist:
            print("\n(очередь не записана: добавьте --persist)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
