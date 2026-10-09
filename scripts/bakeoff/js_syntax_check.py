"""Проверка синтаксиса встроенных скриптов страниц.

Повод: правки страниц через текстовые замены дважды ломали JavaScript («Missing catch or finally
after try»), и страница молча показывала пустые панели со спиннером. Браузер сообщает об этом
только при загрузке, поэтому проверять надо до выката.

Что делает: вытаскивает все <script> без src из каждого файла и проверяет синтаксис:
  * если есть node — `node --check` (полноценный разбор);
  * иначе — грубая проверка баланса скобок и пар try/catch (лучше, чем ничего).

Запуск: python scripts/bakeoff/js_syntax_check.py [каталог static]
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def node_available() -> bool:
    return shutil.which("node") is not None


def check_with_node(code: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = f.name
    res = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    return "" if res.returncode == 0 else (res.stderr or res.stdout).strip().splitlines()[0][:200]


def check_rough(code: str) -> str:
    """Грубая проверка без node: баланс скобок и незакрытые try."""
    stripped = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", code, flags=re.S))
    for open_ch, close_ch in (("{", "}"), ("(", ")"), ("[", "]")):
        if stripped.count(open_ch) != stripped.count(close_ch):
            return f"несбалансированные {open_ch}{close_ch}: {stripped.count(open_ch)} против {stripped.count(close_ch)}"
    if len(re.findall(r"\btry\b", stripped)) != len(re.findall(r"\bcatch\b", stripped)) + len(re.findall(r"\bfinally\b", stripped)):
        return "try без catch/finally"
    return ""


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else
                        pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "static")
    checker = check_with_node if node_available() else check_rough
    print("проверка:", "node --check" if node_available() else "грубая (node не найден)")

    problems, checked = [], 0
    for page in sorted(root.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        for i, block in enumerate(SCRIPT.findall(text), 1):
            if not block.strip():
                continue
            checked += 1
            err = checker(block)
            if err:
                problems.append(f"{page.name} (скрипт {i}): {err}")

    print(f"проверено скриптов: {checked}")
    if problems:
        print("ОШИБКИ:")
        for p in problems:
            print("  -", p)
        return 1
    print("Синтаксис встроенных скриптов в порядке.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
