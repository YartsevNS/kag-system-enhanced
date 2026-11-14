"""Сборка схемы онтологии из ядра и доменных модулей (ADR-010).

    python scripts/build_schema.py --db cybersecurity_ontology_v24
    python scripts/build_schema.py --db ... --modules sec --dry-run

Что делает:
  1. Читает src/ontology/core.tql и выбранные модули src/ontology/modules/<домен>.tql.
  2. Проверяет, что в ядре нет доменных имён (sec_, it_, security, cii_, bdu_).
  3. Проверяет, что один тип не объявлен дважды (иначе TypeDB молча переопределит).
  4. Создаёт базу при необходимости и загружает схемные файлы одной схемой-транзакцией.
  5. Печатает счётчики типов и ролей — по ним видно, что модуль «дошёл».

Запуск внутри контейнера kag-api (там есть драйвер TypeDB и доступ к серверу).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ONTOLOGY = ROOT / "src" / "ontology"
CORE = ONTOLOGY / "core.tql"
MODULES = ONTOLOGY / "modules"

# Имена, которых не должно быть в ядре: префиксы доменов и слова конкретного домена.
FORBIDDEN_IN_CORE = re.compile(r"\b(sec_|it_|security|cii_|bdu)", re.I)

# Объявления типов: entity/relation/attribute <имя> ...
# Объявление типа — это `entity X sub|@abstract|,|;`, а НЕ `entity X plays ...`
DECL = re.compile(r"(?m)^\s*(entity|relation|attribute)\s+([a-zA-Z0-9_-]+)\s*(?:sub|@abstract|[,;])")
# Тип может встречаться повторно как «<имя> sub ...» (строка без ключевого слова) — считаем отдельно.
SUBTYPE_LINE = re.compile(r"(?m)^\s*([a-zA-Z0-9_-]+)\s+(?:sub|@abstract)")


def read(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"нет файла схемы: {path}")
    return path.read_text(encoding="utf-8")


def module_paths(names: list[str]) -> list[Path]:
    out: list[Path] = []
    for name in names:
        p = MODULES / f"{name}.tql"
        if not p.exists():
            raise SystemExit(f"нет модуля: {p}")
        out.append(p)
    return out


def strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def check_core_clean(core: str) -> list[str]:
    problems = []
    for i, line in enumerate(core.splitlines(), 1):
        code = line.split("#", 1)[0]
        if FORBIDDEN_IN_CORE.search(code):
            problems.append(f"core.tql:{i}: доменное имя в ядре — {line.strip()[:80]}")
    return problems


def check_duplicates(parts: dict[str, str]) -> list[str]:
    seen: dict[str, str] = {}
    problems = []
    for name, text in parts.items():
        for _, type_name in DECL.findall(strip_comments(text)):
            if type_name in seen:
                problems.append(f"тип «{type_name}» объявлен дважды: {seen[type_name]} и {name}")
            else:
                seen[type_name] = name
    return problems


def counts(text: str) -> dict[str, int]:
    clean = strip_comments(text)
    return {
        "entity": len(re.findall(r"(?m)^\s*entity\s+", clean)),
        "relation": len(re.findall(r"(?m)^\s*relation\s+", clean)),
        "attribute": len(re.findall(r"(?m)^\s*attribute\s+", clean)),
        "plays": clean.count("plays "),
    }


def load(dbname: str, schema_text: str) -> None:
    from typedb.driver import TransactionType

    from src.indexing import typedb_facts

    cfg = typedb_facts._cfg()
    from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TypeDB

    drv = TypeDB.driver(cfg["addr"], Credentials(cfg["user"], cfg["pass"]),
                        DriverOptions(DriverTlsConfig.disabled()))
    existing = [d.name for d in drv.databases.all()]
    if dbname not in existing:
        drv.databases.create(dbname)
        print(f"создана база {dbname}")
    else:
        print(f"база уже есть: {dbname}")
    with drv.transaction(dbname, TransactionType.SCHEMA) as tx:
        tx.query(schema_text).resolve()
        tx.commit()
    print(f"схема загружена в {dbname}, знаков: {len(schema_text)}")

    # Проверка состава: сколько типов реально в базе
    with drv.transaction(dbname, TransactionType.READ) as tx:
        rows = list(tx.query('match $t sub $r; fetch { "t": $t };').resolve())
    kinds: dict[str, int] = {}
    for r in rows:
        t = r.get("t") or {}
        if isinstance(t, dict) and t.get("label"):
            kinds[t.get("kind", "?")] = kinds.get(t.get("kind", "?"), 0) + 1
    print("типы в базе:", kinds)
    drv.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Сборка схемы из ядра и модулей")
    ap.add_argument("--db", default="cybersecurity_ontology_v24")
    ap.add_argument("--modules", default="sec,it", help="через запятую: sec,it")
    ap.add_argument("--dry-run", action="store_true", help="только проверки, без загрузки")
    ap.add_argument("--dump", help="куда сохранить собранный текст схемы")
    a = ap.parse_args()

    core = read(CORE)
    modules = module_paths([m.strip() for m in a.modules.split(",") if m.strip()])
    parts = {"core": core}
    for p in modules:
        parts[p.stem] = read(p)

    problems = check_core_clean(core) + check_duplicates(parts)
    if problems:
        print("ПРОВЕРКИ НЕ ПРОЙДЕНЫ:")
        for p in problems:
            print("  -", p)
        return 2

    print("проверки пройдены: ядро без доменных имён, дублей типов нет")
    for name, text in parts.items():
        c = counts(text)
        print(f"  {name:8} entity {c['entity']:>3} · relation {c['relation']:>3} "
              f"· attribute {c['attribute']:>3} · plays {c['plays']:>3}")

    # В каждом файле своя шапка `define` — в собранной схеме он должен быть ОДИН.
    cleaned: list[str] = []
    for i, text in enumerate(parts.values()):
        body = text.strip()
        if body.lower().startswith("define"):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        cleaned.append(body.strip())
    schema_text = "define\n" + "\n\n".join(cleaned)
    if a.dump:
        Path(a.dump).write_text(schema_text, encoding="utf-8")
        print(f"собранная схема сохранена: {a.dump}")
    if a.dry_run:
        print("dry-run: загрузка пропущена")
        return 0

    load(a.db, schema_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
