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
TABLE_LIKE_RATIO = 0.3      # доля строк с признаками таблицы: похоже на таблицу без разметки
MIN_TABLE_LINES = 3         # и таких строк должно быть не меньше — иначе это колонтитулы и номера страниц
GARBAGE_MAX = 0.05          # доля «мусорных» символов: текстовый слой повреждён
DENSE_TABLE_RATIO = 0.5     # «плотная» таблица в тексте без разметки (для страниц с текстовым слоем)          # доля «мусорных» символов: текстовый слой повреждён (кодировка, OCR)

ROUTE_PARSER = "parser"          # обычный парсер (модель не нужна)
ROUTE_OCR = "ocr"                # наш OCR (текст есть, таблиц нет)
ROUTE_VLM = "vlm_tables"         # страницу отдаём модели для восстановления таблиц
ROUTE_SKIP = "skip"              # уже обработана моделью — повторно не гоняем

# Символы, которые считаем «нормальным» текстом. Всё прочее — мусор от кодировки или OCR.
_OK_CHARS = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя" "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789 \t\n\r.,;:!?()[]{}<>«»\"'`~@#$%^&*_+=/\\|-–—№°")
_NUMERICISH = set("0123456789.,%-–—")


def table_like_stats(text: str) -> tuple[float, int]:
    """Признаки табличности: (доля табличных строк, их количество).

    Количество нужно отдельно от доли: три короткие числовые строки на страницу — это колонтитулы и номера
    страниц, а не таблица. Поэтому решение требует и доли, и абсолютного минимума строк
    (см. MIN_TABLE_LINES). Первый прогон по корпусу показал, почему: без этого признака в модель уходило
    22% всех фрагментов (2206 из 9931) — то есть четверть корпуса и шесть суток работы на CPU.

    Признаки строки (взяты из живого OCR-текста таблиц):
      * два и более числовых токена — «Блок детектирования … 4 … 28»;
      * короткая строка, состоящая только из числа — так отдаются отдельные ячейки («43», «12», «138»).
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return 0.0, 0

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
    return round(like / len(lines), 3), like


def table_like_ratio(text: str) -> float:
    """Доля табличных строк (совместимость: используется в отчётах и тестах)."""
    return table_like_stats(text)[0]


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
    table_like: float = 0.0                    # доля табличных строк
    table_like_lines: int = 0                  # сколько таких строк (защита от колонтитулов)
    garbage: float = 0.0                       # доля мусорных символов
    already_vlm: bool = False                  # страница уже восстанавливалась моделью
    # Сигналы уровня ДОКУМЕНТА: если таблицы документа уже разобраны с приемлемым качеством,
    # модель не нужна ни одной его странице — именно это убирает основную массу ложных срабатываний
    # (первый прогон по корпусу без этого сигнала дал 22% фрагментов в модель).
    doc_tables_count: int = 0
    doc_tables_quality: Optional[float] = None
    doc_is_scan: bool = False                  # картинка или скан (file_type image/* или PDF без текстового слоя)
    notes: List[str] = field(default_factory=list)

    @classmethod
    def from_text(cls, page: int, text: str, tables_found: int = 0,
                  worst_quality: Optional[float] = None,
                  already_vlm: bool = False,
                  doc_tables_count: int = 0,
                  doc_tables_quality: Optional[float] = None,
                  doc_is_scan: bool = False) -> "PageSignals":
        """Собрать сигналы из текста страницы одной строкой — так это делает вызывающий код."""
        ratio, lines = table_like_stats(text)
        return cls(page=page, text_chars=len(text or ""), tables_found=tables_found,
                   worst_quality=worst_quality, table_like=ratio, table_like_lines=lines,
                   garbage=garbage_ratio(text), already_vlm=already_vlm,
                   doc_tables_count=doc_tables_count, doc_tables_quality=doc_tables_quality,
                   doc_is_scan=doc_is_scan)


def decide_route(sig: PageSignals) -> Dict[str, Any]:
    """Решение по странице: маршрут + причина (причина всегда сохраняется, её видно админу)."""
    if sig.already_vlm:
        return {"page": sig.page, "route": ROUTE_SKIP,
                "reason": "страница уже восстановлена моделью"}

    # Сигнал уровня документа идёт ПЕРВЫМ: если таблицы документа разобраны с приемлемым качеством,
    # модель ему не нужна вообще. Без этой проверки первый прогон по корпусу отправлял в модель 22%
    # фрагментов (2206 из 9931) — потому что «числовые» строки есть почти в любом документе.
    if (sig.doc_tables_count > 0 and sig.doc_tables_quality is not None
            and sig.doc_tables_quality >= TABLE_QUALITY_MIN):
        return {"page": sig.page, "route": ROUTE_PARSER,
                "reason": (f"у документа {sig.doc_tables_count} разобранных таблиц, "
                           f"качество {sig.doc_tables_quality:.2f} — модель не нужна")}

    # Табличность требуем и по доле, и по количеству строк: три числовые строки — это колонтитулы.
    looks_like_table = sig.tables_found > 0 or (
        sig.table_like > TABLE_LIKE_RATIO and sig.table_like_lines >= MIN_TABLE_LINES)

    # Скан или картинка: тип файла — надёжный признак, в отличие от длины текста (у скана OCR даёт
    # длинный текст, поэтому «мало символов» ловит только страницы, где распознавание ничего не дало).
    if sig.doc_is_scan:
        if looks_like_table:
            return {"page": sig.page, "route": ROUTE_VLM,
                    "reason": (f"скан или картинка с признаками таблицы (строк {sig.table_like_lines}, "
                               f"доля {sig.table_like:.2f})")}
        return {"page": sig.page, "route": ROUTE_OCR,
                "reason": "скан или картинка без признаков таблицы — наш OCR"}

    no_text_layer = sig.text_chars < MIN_TEXT_CHARS
    if no_text_layer:
        if looks_like_table:
            return {"page": sig.page, "route": ROUTE_VLM,
                    "reason": (f"нет текстового слоя ({sig.text_chars} симв.) и признаки таблицы "
                               f"(строк {sig.table_like_lines}, доля {sig.table_like:.2f})")}
        return {"page": sig.page, "route": ROUTE_OCR,
                "reason": f"нет текстового слоя ({sig.text_chars} симв.), таблиц не видно — наш OCR"}

    # Дальше — страницы С текстовым слоем. Модель им нужна только в одном случае: если у документа
    # вообще нет разобранных таблиц, а текст явно табличный (плотная сетка чисел). Намеренно НЕ
    # отправляем в модель из-за «низкого качества» разметки: прогон по корпусу 26.09.2026 показал,
    # что такое правило затягивает в модель 35% фрагментов (3455 из 9931) — потому что большинство
    # «таблиц» в цифровых PDF это шум, который нам не нужен, и низкое качество там нормально.
    if (sig.doc_tables_count == 0 and sig.tables_found == 0
            and sig.table_like > DENSE_TABLE_RATIO and sig.table_like_lines >= MIN_TABLE_LINES):
        return {"page": sig.page, "route": ROUTE_VLM,
                "reason": (f"плотный табличный текст без разметки: строк {sig.table_like_lines}, "
                           f"доля {sig.table_like:.2f}, у документа таблиц нет")}

    # Повреждённый текстовый слой лечится повторным распознаванием (у нас есть починка кодировки
    # и OCR), а не табличной моделью: проблема в тексте, а не в структуре таблицы.
    if sig.garbage > GARBAGE_MAX:
        return {"page": sig.page, "route": ROUTE_OCR,
                "reason": f"текстовый слой повреждён (мусорных символов {sig.garbage:.1%}) — повторное распознавание"}

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
