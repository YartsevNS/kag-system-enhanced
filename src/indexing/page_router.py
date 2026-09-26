"""Паспорт страницы и маршрутизация обработки.

Зачем: VLM (Qwen2-VL-2B) отлично восстанавливает таблицы, но работает секунды-минуты на страницу.
Гонять через неё весь корпус бессмысленно — большинство страниц цифровых PDF разбирается обычным
парсером за миллисекунды. Поэтому решение принимается ПО СТРАНИЦЕ, с сохранением причины: админ
должен видеть, почему страница пошла (или не пошла) в модель.

Замеры, из которых взяты пороги (26.09.2026, сервер моделей 41):
  * Qwen2-VL-2B: компактная таблица 790×796 — правильная разметка за 4 минуты; полная страница А4
    (1241×1754) — мусор (упирается в бюджет визуальных токенов) → значит страницу режем на фрагменты;
  * цифровой PDF со разметкой таблиц модель не требует вовсе — обычный парсер даёт точный результат;
  * таблицы с низким `quality` в слое — это как раз те, где разметка разъехалась (объединённые ячейки,
    нет границ) — их и надо отдавать модели.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Пороги (обоснование — замеры выше и правила из docs/guides/table-recovery-and-export.md) ──
MIN_TEXT_CHARS = 200        # меньше — страница без текстового слоя (скан или картинка)
TABLE_QUALITY_MIN = 0.6     # ниже — разметка таблицы разъехалась, восстанавливаем моделью
TABLE_LIKE_RATIO = 0.3      # доля строк с двумя и более числами: похоже на таблицу без разметки
GARBAGE_MAX = 0.05          # доля «мусорных» символов: текстовый слой повреждён (кодировка, OCR)

ROUTE_PARSER = "parser"          # обычный парсер (модель не нужна)
ROUTE_OCR = "ocr"                # наш OCR (текст есть, таблиц нет)
ROUTE_VLM = "vlm_tables"         # страницу отдаём модели для восстановления таблиц
ROUTE_SKIP = "skip"              # уже обработана моделью — повторно не гоняем

# Символы, которые считаем «нормальным» текстом. Всё прочее — мусор от кодировки или OCR.
_OK_CHARS = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя" "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789 \t\n\r.,;:!?()[]{}<>«»\"'`~@#$%^&*_+=/\\|-–—№°")
_NUMERICISH = set("0123456789.,%-–—")


def table_like_ratio(text: str) -> float:
    """Доля строк, похожих на строку таблицы.

    Признаки взяты из живого OCR-текста таблиц (реальные документы 26.09.2026):
      * в строке два и более числовых токена — «Блок детектирования … 4 … 28»;
      * короткая строка, состоящая только из числа — так OCR отдаёт отдельные ячейки
        (в перечне приборов числа «43», «12», «138» шли каждая на своей строке);
      * короткая строка из одного-двух слов без цифр подряд с числовыми строками тоже характерна
        для таблиц, но её отдельно не считаем — чтобы не ловить заголовки в обычном тексте.
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return 0.0

    def _is_number(tok: str) -> bool:
        return bool(tok) and any(ch.isdigit() for ch in tok) and all(ch in _NUMERICISH for ch in tok)

    like = 0
    for line in lines:
        tokens = line.split()
        numeric = sum(1 for t in tokens if _is_number(t))
        if numeric >= 2:
            like += 1
        elif numeric == 1 and len(line) <= 12:
            like += 1          # отдельная ячейка с числом
    return round(like / len(lines), 3)


def garbage_ratio(text: str) -> float:
    """Доля символов, которых не бывает в нормальном тексте (артефакты кодировки и OCR).

    Реальный случай 2026-09-14: у пяти документов текст был записан неверной кодировкой
    («ÔÅÄÅÐÀËÜÍÎÅ» вместо «ФЕДЕРАЛЬНОЕ»), и поиск по русским запросам не находил их вообще.
    Такие страницы имеет смысл отдать OCR/модели, а не доверять текстовому слою.
    """
    chars = [ch for ch in (text or "") if not ch.isspace()]
    if not chars:
        return 0.0
    bad = sum(1 for ch in chars if ch not in _OK_CHARS)
    return round(bad / len(chars), 4)


@dataclass
class PageSignals:
    """Что известно о странице к моменту решения (заполняется парсером и табличным слоем)."""
    page: int
    text_chars: int = 0
    tables_found: int = 0
    worst_quality: Optional[float] = None      # худшее качество среди таблиц страницы
    table_like: float = 0.0                    # table_like_ratio по тексту страницы
    garbage: float = 0.0                       # garbage_ratio по тексту страницы
    already_vlm: bool = False                  # страница уже восстанавливалась моделью
    notes: List[str] = field(default_factory=list)

    @classmethod
    def from_text(cls, page: int, text: str, tables_found: int = 0,
                  worst_quality: Optional[float] = None,
                  already_vlm: bool = False) -> "PageSignals":
        """Собрать сигналы из текста страницы одной строкой — так это делает вызывающий код."""
        return cls(page=page, text_chars=len(text or ""), tables_found=tables_found,
                   worst_quality=worst_quality, table_like=table_like_ratio(text),
                   garbage=garbage_ratio(text), already_vlm=already_vlm)


def decide_route(sig: PageSignals) -> Dict[str, Any]:
    """Решение по странице: маршрут + причина (причина всегда сохраняется, её видно админу)."""
    if sig.already_vlm:
        return {"page": sig.page, "route": ROUTE_SKIP,
                "reason": "страница уже восстановлена моделью"}

    no_text_layer = sig.text_chars < MIN_TEXT_CHARS
    looks_like_table = sig.tables_found > 0 or sig.table_like > TABLE_LIKE_RATIO

    if no_text_layer:
        if looks_like_table:
            return {"page": sig.page, "route": ROUTE_VLM,
                    "reason": (f"нет текстового слоя ({sig.text_chars} симв.) и есть признаки таблицы "
                               f"(табличный текст {sig.table_like:.2f})")}
        return {"page": sig.page, "route": ROUTE_OCR,
                "reason": f"нет текстового слоя ({sig.text_chars} симв.), таблиц не видно — наш OCR"}

    if sig.worst_quality is not None and sig.worst_quality < TABLE_QUALITY_MIN:
        return {"page": sig.page, "route": ROUTE_VLM,
                "reason": (f"разметка таблицы низкого качества "
                           f"({sig.worst_quality:.2f} < {TABLE_QUALITY_MIN})")}

    if sig.tables_found == 0 and sig.table_like > TABLE_LIKE_RATIO:
        return {"page": sig.page, "route": ROUTE_VLM,
                "reason": f"табличный текст без разметки (доля табличных строк {sig.table_like:.2f})"}

    if sig.garbage > GARBAGE_MAX:
        return {"page": sig.page, "route": ROUTE_VLM,
                "reason": f"текстовый слой повреждён (мусорных символов {sig.garbage:.1%})"}

    if sig.tables_found > 0:
        return {"page": sig.page, "route": ROUTE_PARSER,
                "reason": f"таблицы размечены, качество приемлемо (нашлось таблиц: {sig.tables_found})"}

    return {"page": sig.page, "route": ROUTE_PARSER, "reason": "обычная текстовая страница"}


def route_document(pages: List[PageSignals]) -> Dict[str, Any]:
    """Сводка по документу: что делать со страницами и сколько их уйдёт в модель.

    Нужно, чтобы до обработки показать цену решения: модель — это секунды-минуты на страницу.
    """
    decisions = [decide_route(p) for p in pages]
    counts: Dict[str, int] = {}
    for d in decisions:
        counts[d["route"]] = counts.get(d["route"], 0) + 1
    return {
        "pages": len(pages),
        "by_route": counts,
        "vlm_pages": [d["page"] for d in decisions if d["route"] == ROUTE_VLM],
        "decisions": decisions,
    }
