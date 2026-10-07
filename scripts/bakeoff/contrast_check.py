"""Проверка контраста палитры KAG по WCAG 2.1.

Читает переменные темы прямо из src/api/static/theme.css и считает коэффициент контраста
для пар «текст на фоне», которые реально используются в интерфейсе. Порог: 4.5:1 для основного
текста, 3:1 для служебных подписей и элементов управления.

Запуск:  python scripts/bakeoff/contrast_check.py
"""
from __future__ import annotations

import pathlib
import re
import sys

CSS = pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "static" / "theme.css"


def parse_vars(text: str, selector: str) -> dict[str, str]:
    """Достаёт переменные из блока нужного селектора."""
    start = text.index(selector)
    block = text[start:text.index("}", start)]
    return {name: val.strip() for name, val in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


def to_rgb(value: str) -> tuple[int, int, int] | None:
    v = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", v)
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", v)
    if m:
        return tuple(int(g) for g in m.groups())  # type: ignore[return-value]
    return None


def luminance(rgb: tuple[int, int, int]) -> float:
    def ch(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(float(x)) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg: str, bg: str) -> float | None:
    a, b = to_rgb(fg), to_rgb(bg)
    if not a or not b:
        return None
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


PAIRS = [           # (что, цвет текста, цвет фона, порог)
    ("основной текст",            "--text",         "--bg",     4.5),
    ("основной текст на карточке", "--text",        "--surface", 4.5),
    ("вторичный текст",           "--text2",        "--bg",     4.5),
    ("третичный текст",           "--text3",        "--surface", 4.5),
    ("служебная подпись",         "--text4",        "--surface", 3.0),
    ("акцент как текст",          "--accent",       "--surface", 4.5),
    ("текст на акцентной заливке", "--on-accent",    "--accent-solid", 4.5),
    ("активный пункт меню",       "--accent",       "--accent-tint", 4.5),
    ("пункт меню (обычный)",      "--text2",        "--panel",  4.5),
]


def main() -> int:
    text = CSS.read_text(encoding="utf-8")
    light = parse_vars(text, ":root {")
    dark = parse_vars(text, ':root[data-theme="dark"] {')

    failures = []
    for name, theme in (("СВЕТЛАЯ", light), ("ТЁМНАЯ", dark)):
        print(f"\n=== {name} тема ===")
        for label, fg_var, bg_var, need in PAIRS:
            if fg_var is None:                       # белое на акценте
                fg, bg = "#FFFFFF", theme["--accent"]
            else:
                fg, bg = theme[fg_var], theme[bg_var]
            r = ratio(fg, bg)
            if r is None:
                print(f"  {label:30} не разобрал цвета ({fg} на {bg})")
                continue
            mark = "ок" if r >= need else "МАЛО"
            print(f"  {label:30} {r:5.2f}:1  (нужно {need}:1)  {mark}")
            if r < need:
                failures.append(f"{name}: {label} — {r:.2f}:1 при нужных {need}:1")

    if failures:
        print("\nНЕ ПРОШЛО:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nВсе пары проходят пороги WCAG.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
