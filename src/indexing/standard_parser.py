"""Извлечение структурных метаданных из текста чанка: номер стандарта и пункт.

Зачем: превратить векторный поиск в структурно-осмысленный — «найди требования
ГОСТ 57580.1-2017, п. 5.2.1», а не «найди похожий текст». Значения кладутся
в payload Qdrant и доступны как фильтры поиска.

Принцип: извлекаем ТОЛЬКО явные упоминания (ГОСТ/СТО/ISO, «п. 5.2.1»), чтобы
не порождать ложных срабатываний на произвольных числах. Если ничего не найдено
— возвращаем None (поле в payload не пишется).
"""

from __future__ import annotations

import re
from typing import Optional

# Номер стандарта. Порядок шаблонов = приоритет (первое совпадение выигрывает).
_STANDARD_PATTERNS = [
    # ГОСТ Р 56545-2015, ГОСТ 57580.1-2017, ГОСТ Р ИСО/МЭК 27001-2021
    re.compile(
        r"\bГОСТ(?:\s+Р)?(?:\s+(?:ИСО|МЭК|IEC|ISO))?(?:/\s*(?:МЭК|IEC|ISO))?"
        r"\s+\d+(?:[.\-]\d+)*(?:-\d{4})?",
        re.IGNORECASE,
    ),
    # СТО БР БФБО-1.9-2024, СТО 34.01-4.1-001-2017
    re.compile(r"\bСТО\s+[А-ЯЁA-Z][\w.\-]*\s+[\w.\-]+(?:-\d{4})?", re.IGNORECASE),
    # ISO/IEC 27001:2022, ISO 9001-2015
    re.compile(r"\bISO(?:/\s*IEC)?\s+\d+(?:[.\-:]\d+)*", re.IGNORECASE),
    # Приказ ФСТЭК России № 17, Приказ ФСБ № 378
    re.compile(r"\bПриказ\s+(?:ФСТЭК|ФСБ)[^.;()}{]{0,40}?№\s*\d+", re.IGNORECASE),
]

# Пункт: «п. 5.2.1», «пункт 5.2.1», «п.п. 5.2.1.3», либо номер в начале строки.
_CLAUSE_PATTERNS = [
    re.compile(r"\b(?:п\.п\.|пункты|пункт|п\.)\s*(\d+(?:\.\d+){1,3})", re.IGNORECASE),
    re.compile(r"^\s*(\d+(?:\.\d+){1,3})[\s.)]", re.MULTILINE),
]

# Раздел/глава — грубее, но полезно для фильтра «раздел 5»
_SECTION_PATTERN = re.compile(r"\b(?:раздел|гл\.|глава)\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


def _clean(value: str) -> str:
    """Нормализовать пробелы (в PDF встречаются переводы строк внутри номера)."""
    return re.sub(r"\s+", " ", value).strip()


def extract_standard_number(text: str) -> Optional[str]:
    """Вернуть первый найденный номер стандарта или None."""
    if not text:
        return None
    for pattern in _STANDARD_PATTERNS:
        m = pattern.search(text)
        if m:
            return _clean(m.group(0))
    return None


def extract_clause(text: str) -> Optional[str]:
    """Вернуть номер пункта («5.2.1») или None."""
    if not text:
        return None
    for pattern in _CLAUSE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


def extract_section(text: str) -> Optional[str]:
    """Вернуть номер раздела/главы («5») или None."""
    if not text:
        return None
    m = _SECTION_PATTERN.search(text)
    return m.group(1) if m else None


def extract_structure(text: str) -> dict:
    """Собрать все структурные поля чанка (только непустые)."""
    result = {}
    std = extract_standard_number(text)
    if std:
        result["standard_number"] = std
    clause = extract_clause(text)
    if clause:
        result["clause"] = clause
    section = extract_section(text)
    if section:
        result["section"] = section
    return result
