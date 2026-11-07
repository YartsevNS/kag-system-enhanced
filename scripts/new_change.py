"""Создать папку изменения по шаблону OpenSpec-структуры.

    python scripts/new_change.py "наполнение онтологии корпуса"
    -> docs/changes/2026-11-07-napolnenie-ontologii-korpusa/

Копирует proposal/specs/design/tasks из _TEMPLATE. result.md не копируется —
он появляется после выполнения, иначе превращается в обязательную формальность.
"""
from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGES = ROOT / "docs" / "changes"
TEMPLATE = CHANGES / "_TEMPLATE"

TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "j", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "ju", "я": "ja",
})


def slugify(text: str) -> str:
    text = text.lower().translate(TRANSLIT)
    out = []
    for ch in text:
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_/":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60] or "change"


def main() -> int:
    if len(sys.argv) < 2:
        print("использование: python scripts/new_change.py \"название изменения\"")
        return 2
    title = " ".join(sys.argv[1:]).strip()
    folder = CHANGES / f"{date.today().isoformat()}-{slugify(title)}"
    if folder.exists():
        print(f"уже существует: {folder}")
        return 1

    folder.mkdir(parents=True)
    for name in ("proposal.md", "specs.md", "design.md", "tasks.md"):
        src = TEMPLATE / name
        text = src.read_text(encoding="utf-8").replace("<название>", title)
        (folder / name).write_text(text, encoding="utf-8")

    print(f"создано: {folder}")
    for f in sorted(folder.iterdir()):
        print("  ", f.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
