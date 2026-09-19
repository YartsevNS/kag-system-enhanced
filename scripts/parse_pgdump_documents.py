#!/usr/bin/env python3
"""Разбор блока COPY public.documents из дампа pg_dump (backup_20260829/kag.sql).

Пишет JSON {filename: {колонка: значение}} в файл, указанный вторым аргументом.
Запуск: python3 parse_dump_docs.py <dump.sql> <out.json>
"""
import json
import re
import sys
from pathlib import Path

ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "\\": "\\", '"': '"', "'": "'"}


def unescape(value: str):
    if value == "\\N":
        return None
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(ESCAPES.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def main() -> int:
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    text = src.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"COPY public\.documents \(([^)]*)\) FROM stdin;\n(.*?)\n\\\.\n", text, re.S)
    if not m:
        print("НЕ НАЙДЕН блок COPY public.documents")
        return 1
    cols = [c.strip() for c in m.group(1).split(",")]
    rows = []
    for line in m.group(2).split("\n"):
        if not line.strip():
            continue
        vals = line.split("\t")
        rows.append({c: unescape(v) for c, v in zip(cols, vals)})
    by_name = {}
    for r in rows:
        name = r.get("filename")
        if name:
            by_name[name] = r
    dst.write_text(json.dumps(by_name, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"колонок: {len(cols)} | строк: {len(rows)} | уникальных имён файлов: {len(by_name)}")
    print("колонки:", ", ".join(cols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
