"""Детерминированные извлекатели для «скелета» графа: без модели, без токенов.

Зачем: значительная часть нужных нам связей — не смысловые, а справочные, и извлекаются правилами
с высокой точностью: номера ГОСТ и СП, ссылки на пункты, даты, номера документов, номера законов.
Такие связи (документ ссылается на ГОСТ, в документе есть пункт 5.2.1) сейчас в графе отсутствуют
вовсе, хотя именно их чаще всего и спрашивают. Стоимость — ноль токенов.

Границы применимости (проверено по источникам): правилами НЕЛЬЗЯ заменять извлечение смысловых
связей — на русских нормативных текстах end-to-end извлечение отношений даёт F1 0,062 (RuREBus).
Здесь только то, что имеет строгую форму записи.

Каждый результат несёт свою природу: link="regex" и версия извлекателя — чтобы в графе всегда было
видно, что связь получена правилом, а не моделью (иначе оценки качества смешаются).

Модуль намеренно без внешних зависимостей — работает и на стенде, и на ноутбуке.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EXTRACTOR_VERSION = "regex-v1"

# ── Номера стандартов ────────────────────────────────────────────────────────────
# ГОСТ Р 56545-2015, ГОСТ 34.601-90, ГОСТ Р ИСО/МЭК 27001-2021, ГОСТ Р 50922-2006
_GOST = re.compile(
    r"\bГОСТ(?:\s+Р)?(?:\s+ИСО\s*/\s*МЭК)?(?:\s+Р)?\s*"
    r"\d{1,5}(?:[.\-]\d{1,4})*(?:\s*-\s*\d{2,4})?",
    re.IGNORECASE,
)
# СП 500-131-2007, СНиП 2.01.02-85, СанПиН 2.2.2/2.4.1340-03
_SP = re.compile(r"\b(?:СП|СНиП|СанПиН|СН)\s+\d{1,4}(?:[.\-/]\d{1,4})*(?:-\d{2,4})?", re.IGNORECASE)
# 152-ФЗ, 187-ФЗ, 44-ФЗ
_FZ = re.compile(r"\b(\d{2,3})\s*-\s*ФЗ\b", re.IGNORECASE)

# ── Номера документов ───────────────────────────────────────────────────────────
# Приказ ФСТЭК России № 17, Постановление Правительства РФ № 1119, Положение № 123-П
_DOC_NUMBER = re.compile(
    r"\b(Приказ|Положение|Распоряжение|Указание|Постановление|Письмо|Инструкция|Методика)"
    r"[\sА-ЯЁа-яё.]{0,40}?№\s*([0-9А-ЯЁа-яё][0-9А-ЯЁа-яё\-/]{0,15})",
    re.IGNORECASE,
)

# ── Ссылки на пункты и разделы ──────────────────────────────────────────────────
_CLAUSE = re.compile(
    r"\b(п\.|пп\.|пункт|подпункт|раздел|разд\.|глава|таблица|приложение|ст\.|статья|часть|ч\.)"
    r"\s*№?\s*(\d{1,3}(?:\.\d{1,3}){0,4})\b",
    re.IGNORECASE,
)

# Вид ссылки приводим к короткому единому обозначению: без этого «п. 3» и «табл. 3» сливаются.
_CLAUSE_KINDS = {
    "п.": "п.", "пп.": "пп.", "пункт": "п.", "подпункт": "пп.",
    "раздел": "разд.", "разд.": "разд.", "глава": "гл.",
    "таблица": "табл.", "приложение": "прил.",
    "ст.": "ст.", "статья": "ст.", "часть": "ч.", "ч.": "ч.",
}

# ── Даты ────────────────────────────────────────────────────────────────────────
_DATE_DOT = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_DATE_WORDS = re.compile(
    r"\b(\d{1,2})\s+(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|июн\w*|июл\w*|август\w*|"
    r"сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\s+(\d{4})",
    re.IGNORECASE,
)


@dataclass
class Skeleton:
    """Всё, что удалось взять правилами из одного фрагмента."""

    gost: list[str] = field(default_factory=list)
    sp: list[str] = field(default_factory=list)
    fz: list[str] = field(default_factory=list)
    doc_numbers: list[str] = field(default_factory=list)
    clauses: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any((self.gost, self.sp, self.fz, self.doc_numbers, self.clauses, self.dates))

    def total(self) -> int:
        return sum(len(x) for x in (self.gost, self.sp, self.fz, self.doc_numbers, self.clauses, self.dates))


def normalize_gost(raw: str) -> str:
    """Канонический вид номера стандарта: «гост  р 50922 - 2006» → «ГОСТ Р 50922-2006»."""
    t = re.sub(r"\s+", " ", raw.strip()).upper()
    t = re.sub(r"\s*/\s*", "/", t)
    t = re.sub(r"\s*-\s*", "-", t)
    t = re.sub(r"^ГОСТ\s*Р\s*ИСО/МЭК\s*", "ГОСТ Р ИСО/МЭК ", t)
    if not t.startswith("ГОСТ Р ИСО/МЭК"):
        t = re.sub(r"^ГОСТ\s*", "ГОСТ ", t)
    return t


def normalize_clause(raw: str) -> str:
    return re.sub(r"\s+", "", raw)


def normalize_date(day: str, month: str, year: str) -> str | None:
    try:
        d, m, y = int(day), int(month), int(year)
    except ValueError:
        return None
    if not (1 <= d <= 31 and 1 <= m <= 12 and 1900 <= y <= 2100):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def extract(text: str) -> Skeleton:
    """Извлечь из текста всё, что имеет строгую форму записи."""
    if not text:
        return Skeleton()
    out = Skeleton()

    for m in _GOST.finditer(text):
        raw = m.group(0)
        # Отсекаем одинокий «ГОСТ» без номера и мусор длиной 1 символ.
        if sum(ch.isdigit() for ch in raw) >= 2:
            out.gost.append(normalize_gost(raw))
    for m in _SP.finditer(text):
        out.sp.append(re.sub(r"\s+", " ", m.group(0).upper()))
    for m in _FZ.finditer(text):
        out.fz.append(f"{m.group(1)}-ФЗ")
    for m in _DOC_NUMBER.finditer(text):
        kind, num = m.group(1), m.group(2)
        out.doc_numbers.append(f"{kind.capitalize()} № {num}")
    for m in _CLAUSE.finditer(text):
        kind = _CLAUSE_KINDS.get(m.group(1).lower(), "п.")
        out.clauses.append(f"{kind}{normalize_clause(m.group(2))}")
    for m in _DATE_DOT.finditer(text):
        v = normalize_date(m.group(1), m.group(2), m.group(3))
        if v:
            out.dates.append(v)
    for m in _DATE_ISO.finditer(text):
        v = normalize_date(m.group(3), m.group(2), m.group(1))
        if v:
            out.dates.append(v)
    for m in _DATE_WORDS.finditer(text):
        key = m.group(2).lower()
        month = next((v for k, v in _MONTHS.items() if key.startswith(k[:4])), None)
        if month:
            v = normalize_date(m.group(1), str(month), m.group(3))
            if v:
                out.dates.append(v)

    # Внутри фрагмента — без повторов, порядок сохраняем (важен для чтения).
    for name in ("gost", "sp", "fz", "doc_numbers", "clauses", "dates"):
        seen: dict[str, None] = {}
        for v in getattr(out, name):
            seen.setdefault(v, None)
        setattr(out, name, list(seen))
    return out


if __name__ == "__main__":  # быстрая самопроверка на живых формулировках из корпуса
    samples = [
        "Настоящий стандарт устанавливает требования к СЗИ в соответствии с ГОСТ Р 50922-2006 "
        "и ГОСТ Р ИСО/МЭК 27001-2021, п. 5.2.1 и раздел 6.",
        "Согласно п. 4.3.2 ГОСТ 34.601-90 и ст. 5 152-ФЗ от 27.07.2006, организация обязана "
        "уведомить регулятора до 01.03.2026. Приказ ФСТЭК России № 17, п. 14.",
        "СанПиН 2.2.2/2.4.1340-03, таблица 3. Утверждено 12 октября 2020 г.",
    ]
    for s in samples:
        r = extract(s)
        print("текст:", s[:70], "…")
        print("  ГОСТ:", r.gost)
        print("  СП:", r.sp, "| ФЗ:", r.fz)
        print("  док-ты:", r.doc_numbers, "| пункты:", r.clauses)
        print("  даты:", r.dates)
