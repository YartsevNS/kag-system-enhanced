#!/usr/bin/env python
"""Загрузка реестра компании (объекты КИИ, СЗИ, требования, документы) в онтологию.

Использование:
    python scripts/import_company_registry.py registry.json
    python scripts/import_company_registry.py registry.json --dry-run
"""
import argparse
import json
import sys

sys.path.insert(0, "/app")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="путь к JSON-файлу реестра")
    ap.add_argument("--dry-run", action="store_true", help="проверить только структуру файла")
    args = ap.parse_args()

    with open(args.path, encoding="utf-8") as fh:
        data = json.load(fh)

    summary = {k: len(data.get(k) or []) for k in
               ("objects", "software", "requirements", "protects", "documents")}
    print("в файле:", summary)
    if args.dry_run:
        return 0

    from src.indexing.company_registry import CompanyRegistry

    result = CompanyRegistry().load(data)
    print("загружено:", result)
    return 0 if result.get("skipped", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
