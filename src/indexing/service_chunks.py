"""Служебные фрагменты документа: титул, оглавление, предисловие, колонтитул.

Зачем. Оценка качества ответов (2026-09-13) показала: слабые ответы — это честное «в контексте
этого нет», и причина не в модели, а в выдаче: в контекст чата приходят титульные листы и
содержание вместо тела документа. Пример ответа системы: «в загруженном контексте представлены
главным образом титульные листы, предисловие и содержание Р 50.1.112—2016 — то есть структура
требований, но не их полный текст».

Такие фрагменты нужны в индексе (по ним ищут номер документа), но в контексте ответа они
занимают место, которое должно достаться содержанию. Поэтому: оставляем их в индексе,
но при сборке контекста чата отдаём приоритет содержательным фрагментам, а служебными
добираем только если содержательных не хватило.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# «.......... 42» — оглавление
_DOTS = re.compile(r"\.{4,}")
# Строка только из номера страницы
_PAGE_ONLY_LINE = re.compile(r"^\s*\d{1,4}\s*$")
# Слова-маркеры служебных частей
_SERVICE_WORDS = (
    "содержание", "оглавление", "предисловие", "титульный лист",
    "стандартинформ", "все права защищены", "© ", "воспроизведен",
)
# Ключевые слова документа
_STD_HINT = re.compile(
    r"\b(гост|госстандарт|рекомендации по стандартизации|информационное письмо|указание|"
    r"методические рекомендации|стандарт банка россии|сто бр)\b",
    re.IGNORECASE,
)


def is_service_fragment(text: str) -> bool:
    """Похож ли фрагмент на служебный (титул/оглавление/предисловие/колонтитул)."""
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]

    if any(w in low for w in _SERVICE_WORDS):
        # «содержание» в теле нормативного текста тоже встречается, поэтому требуем
        # ещё и структурный признак: короткие строки или точки-отбивка.
        if len(_DOTS.findall(t)) >= 2 or (lines and sum(len(ln) < 30 for ln in lines) >= max(3, len(lines) // 2)):
            return True

    # Оглавление: много точек-отбивки и короткие строки
    if len(_DOTS.findall(t)) >= 3:
        short = sum(1 for ln in lines if len(ln) < 30)
        if lines and short >= max(2, len(lines) // 2):
            return True

    # Титул/шапка: строки короткие, предложений нет, объём небольшой. Отличие от обычного
    # абзаца — отсутствие предложений: на титуле «ФЕДЕРАЛЬНОЕ АГЕНТСТВО…», «Р 50.1.112—2016»,
    # а в содержательном тексте есть «. » и заглавная после точки.
    long_lines = sum(1 for ln in lines if len(ln) > 80)
    if lines and len(t) < 700 and long_lines == 0 and len(lines) >= 2:
        shortish = sum(1 for ln in lines if len(ln) < 60)
        no_sentences = not any((". " in ln) or ln.endswith((".", "!", "?")) for ln in lines)
        if no_sentences and shortish >= len(lines) * 0.6:
            return True

    # Колонтитул: обозначение документа + номера страниц, без содержательных предложений
    page_lines = sum(1 for ln in lines if _PAGE_ONLY_LINE.match(ln))
    if page_lines >= 2 and len(t) < 400:
        return True
    if page_lines >= 1 and len(t) < 200 and _STD_HINT.search(t):
        return True

    return False


def split_service(results: List[Dict[str, Any]],
                  content_key: str = "content") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Разделить выдачу на (содержательные, служебные), сохранив порядок."""
    substantive: List[Dict[str, Any]] = []
    service: List[Dict[str, Any]] = []
    for r in results or []:
        (service if is_service_fragment(str((r or {}).get(content_key) or "")) else substantive).append(r)
    return substantive, service


def order_context(results: List[Dict[str, Any]], min_substantive: int = 3,
                  content_key: str = "content") -> Tuple[List[Dict[str, Any]], int]:
    """Порядок для контекста: сначала содержательные, служебными добираем при нехватке.

    Возвращает (упорядоченный список, сколько служебных отложено в конец).
    Никогда не выбрасывает результаты совсем: если содержательных меньше min_substantive,
    добавляем служебные (они всё равно лучше, чем пустой контекст).
    """
    substantive, service = split_service(results, content_key)
    if not substantive:
        return list(results or []), 0
    if len(substantive) < min_substantive:
        return substantive + service, 0
    return substantive + service, len(service)
