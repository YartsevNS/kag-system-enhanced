"""Проверка целостности разметки страниц KAG после правок меню/шапок.

Зачем: замена списков меню регулярными выражениями однажды оставила на 12 страницах обрубок
ссылки «Выйти» (виден как текст `se" style="margin-top:auto..."> Выйти`). Глазами такое ловится
случайно, скриптом — всегда.

Что проверяется:
  1. нет обрубков атрибутов (текст вида `se" style=` не внутри тега);
  2. число открывающих и закрывающих ссылок совпадает;
  3. на страницах с боковой колонкой есть рабочая ссылка выхода (кроме публичных/мастер-страниц);
  4. нет пустых элементов вместо значков (`<span></span>`, `<button></button>` без подписи).

Запуск: python scripts/bakeoff/markup_sanity.py
"""
from __future__ import annotations

import pathlib
import re
import sys

STATIC = pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "static"

# страницы, где выхода быть не должно: публичная схема и мастер первичной настройки
NO_LOGOUT = {"architecture.html", "setup.html", "login.html", "know.html", "prompts-help.html"}
# страницы без боковой колонки — проверка выхода к ним не относится
NO_SIDEBAR = {"chat.html", "login.html", "know.html", "prompts-help.html", "qdrant.html", "system.html"}

PATTERNS = {
    # Настоящий обрубок: тег закрылся, а дальше в тексте висит кусок атрибута
    # (например `</div>se" style="margin-top:auto"> Выйти`). Нормальные атрибуты внутри тега
    # под этот шаблон не попадают — у них перед словом стоит пробел, а не `>`.
    "обрубок атрибута": re.compile(r'>\s*[A-Za-zА-Яа-я_]{1,24}"\s+style\s*='),
    "незакрытая ссылка": re.compile(r"<a\b(?:(?!</a>).){0,400}$", re.S),
    "пустой значок": re.compile(r'<span class="icon">\s*</span>'),
    "пустая кнопка": re.compile(r"<button[^>]*>\s*</button>"),
}


def markup_only(html: str) -> str:
    """Оставляет только разметку: без скриптов, стилей и комментариев.

    Иначе проверка ловит упоминания тегов внутри JS/CSS (например, комментарий «...и как <a>»),
    и настоящие обрубки тонут в ложных срабатываниях.
    """
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def main() -> int:
    problems: list[str] = []
    for f in sorted(STATIC.glob("*.html")):
        t = markup_only(f.read_text(encoding="utf-8"))
        for name, rx in PATTERNS.items():
            if name == "незакрытая ссылка":
                continue                     # проверяем счётчиком ниже
            found = rx.findall(t)
            if found:
                problems.append(f"{f.name}: {name} — {len(found)} шт., например {found[0][:60]!r}")

        opens = len(re.findall(r"<a\b", t))
        closes = len(re.findall(r"</a>", t))
        if opens != closes:
            problems.append(f"{f.name}: ссылок открыто {opens}, закрыто {closes}")

        nav = re.search(r"<nav[^>]*>(.*?)</nav>", t, re.S)
        if nav and f.name not in NO_SIDEBAR and f.name not in NO_LOGOUT:
            if 'onclick="logout()' not in nav.group(1):
                problems.append(f"{f.name}: в боковой колонке нет ссылки выхода")

        if f.name not in NO_SIDEBAR and "data-site-nav" not in t and nav:
            problems.append(f"{f.name}: боковая колонка без общего меню (data-site-nav)")

    print(f"проверено страниц: {len(list(STATIC.glob('*.html')))}")
    if problems:
        print("НАЙДЕНО:")
        for p in problems:
            print("  -", p)
        return 1
    print("Разметка чистая: обрубков нет, ссылки парные, выход на месте, меню общее.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
