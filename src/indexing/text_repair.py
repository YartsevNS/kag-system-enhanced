"""Битый текстовый слой PDF: определение и восстановление кодировки.

Проблема (2026-09-13): у части PDF текстовый слой записан неверной кодировкой — вместо кириллицы
латиница с диакритикой («ÔÅÄÅÐÀËÜÍÎÅ ÀÃÅÍÒÑÒÂÎ» вместо «ФЕДЕРАЛЬНОЕ АГЕНТСТВО»). Такой текст
выглядит для конвейера нормальным (длина достаточная), поэтому OCR не включался, а чанки уходили
в индекс искажёнными: по русским запросам они не находятся вообще.

Замер: 5 документов из 61, 43 чанка; худший — 892c0e3b (27 из 34 чанков искажены), плюс 7465c83e
(11 из 11). Остальные три — единичные чанки с формулами, где слой нормальный.

Как лечим:
* `looks_like_mojibake` — признак: в тексте много последовательностей латинских букв с диакритикой
  и мало настоящей кириллицы;
* `repair_mojibake` — обратное преобразование latin-1 → cp1251 (точное восстановление текста);
* если восстановление не даёт читаемой кириллицы — считаем, что текстового слоя нет, и отдаём
  страницу на OCR (см. parsers.py).
"""

from __future__ import annotations

import re

# Последовательности «латинских букв с диакритикой» — характерный признак cp1251-текста,
# прочитанного как latin-1 (ÔÅÄÅÐÀËÜÍÎÅ, ôóíêöèÿ, åñëè).
_MOJIBAKE_RUN = re.compile(r"[À-ÿ]{3,}")
# Настоящая кириллица
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# Порог: доля кириллицы, ниже которой текст считаем битым
_MIN_CYRILLIC_SHARE = 0.10


def _ascii_and_punct_share(text: str) -> float:
    if not text:
        return 1.0
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 1.0
    latin = sum(1 for ch in letters if "a" <= ch.lower() <= "z")
    return latin / len(letters)


def cyrillic_share(text: str) -> float:
    """Доля кириллицы среди букв текста."""
    letters = [ch for ch in (text or "") if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if _CYRILLIC.match(ch)) / len(letters)


def looks_like_mojibake(text: str) -> bool:
    """Похож ли текст на cp1251, прочитанный как latin-1."""
    t = (text or "").strip()
    if len(t) < 40:
        return False
    runs = _MOJIBAKE_RUN.findall(t)
    # нужно несколько длинных «диакритических» серий и мало настоящей кириллицы
    long_runs = [r for r in runs if len(r) >= 4]
    if len(long_runs) < 3:
        return False
    return cyrillic_share(t) < _MIN_CYRILLIC_SHARE


def repair_mojibake(text: str) -> str:
    """Обратное преобразование latin-1 → cp1251 посимвольно.

    Посимвольно, а не через `text.encode('latin-1')`: в тексте есть символы вне latin-1
    (тире, кавычки, знак номера), из-за которых пакетное кодирование падало с
    UnicodeEncodeError, а текст оставался битым (живой случай: «после ремонта» совпадало
    с исходным).
    """
    out = []
    for ch in text or "":
        code = ord(ch)
        if code < 256:
            try:
                out.append(bytes([code]).decode("cp1251"))
                continue
            except UnicodeDecodeError:
                pass
        out.append(ch)
    return "".join(out)


def needs_ocr(text: str) -> bool:
    """Нужно ли отдать страницу на OCR: битый слой, который не восстанавливается."""
    if not looks_like_mojibake(text):
        return False
    repaired = repair_mojibake(text)
    return cyrillic_share(repaired) < _MIN_CYRILLIC_SHARE
