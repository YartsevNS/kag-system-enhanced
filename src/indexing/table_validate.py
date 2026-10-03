"""Арифметическая проверка таблиц счетов-фактур и накладных (УПД, ТОРГ-12).

Зачем: ни одна публичная метрика не измеряет то, что нам важно. TEDS, GriTS и mAP проверяют структуру и текст,
но не замечают, что «1» распознано как «7», «3» как «8», а значение уехало на колонку влево. На сканах накладных
критична именно верность чисел, поэтому арифметика становится арбитром: нашли строки и итоги — документ можно
принимать, не сошлось — точечная проверка ячеек.

Проверяем (по данным таблицы, без модели):

  * по строке: количество × цена = стоимость (с допуском на копейки);
  * по строке: стоимость + сумма налога = стоимость с налогом;
  * по строке: сумма налога = стоимость × ставка (20 %, 10 %, 0 %), включая вариант «в том числе НДС»;
  * по документу: сумма строк = строка «Итого»/«Всего».

Колонки определяются по тексту шапки, а не по позиции: «Кол-во»/«Количество», «Цена», «Стоимость товаров»,
«Сумма налога», «Стоимость с налогом», «Ставка НДС». Это устойчивее к лишним/пропущенным колонкам и к
объединённым ячейкам шапки, чем привязка к номеру столбца.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

COLUMN_PATTERNS: List[Tuple[str, Tuple[str, ...]]] = [
    ("qty", (r"колич", r"кол-во", r"кол\.", r"объем", r"объём")),
    ("price", (r"цена", r"тариф")),
    ("amount", (r"стоимость товаров", r"стоимость работ", r"стоимость услуг", r"^стоимость$")),
    ("rate", (r"ставка", r"налого-?вая ставка")),
    ("tax", (r"сумма налога", r"сумма ндс", r"^ндс$")),
    ("total", (r"стоимость товаров с налогом", r"с налогом", r"всего с ндс", r"итого с ндс")),
]

AMOUNT_TOLERANCE = 0.05          # допуск на копейки при перемножении
TOTALS_WORDS = (r"^итого$", r"^всего$", r"^итого по", r"^всего по", r"^итого к оплате")


@dataclass
class RowCheck:
    index: int
    status: str                    # ok | mismatch | no_data
    details: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "status": self.status, "details": self.details}


@dataclass
class TableVerdict:
    """Вердикт по таблице: сколько строк проверено и сошлось, что именно не сошлось."""
    checked: int = 0
    ok: int = 0
    rows: List[RowCheck] = field(default_factory=list)
    totals_checked: bool = False
    totals_ok: Optional[bool] = None
    notes: List[str] = field(default_factory=list)
    columns: Dict[str, int] = field(default_factory=dict)

    @property
    def mismatch(self) -> int:
        return sum(1 for r in self.rows if r.status == "mismatch")

    @property
    def verdict(self) -> str:
        if self.checked == 0:
            return "нет данных для проверки"
        if self.mismatch == 0 and (self.totals_ok in (True, None)):
            return "сходится" if self.totals_ok else "строки сходятся, итоги не найдены"
        return f"расхождений: {self.mismatch} из {self.checked}"

    def as_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "checked": self.checked, "ok": self.ok,
                "mismatch": self.mismatch, "totals_ok": self.totals_ok,
                "columns": self.columns, "notes": self.notes,
                "rows": [r.as_dict() for r in self.rows if r.status == "mismatch"][:20]}


def parse_amount(value: Any) -> Optional[float]:
    """Разобрать число из ячейки: «13 959,9», «2 791.98», «43 250,36», «1 873,3», «796».

    Тысячи отделяются пробелом (в том числе неразрывным), дробная часть — запятой или точкой. Кириллические
    «похожие» символы (О, З) не принимаем: если после чистки остались буквы — это не число.
    """
    if value is None:
        return None
    s = str(value).strip().replace("\u00a0", " ").replace("\u2009", " ")
    if not s:
        return None
    s = re.sub(r"[^\d,.\- ]", "", s).strip()
    if not s:
        return None
    s = s.replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")      # 1.234,56 → 1234.56
    elif "," in s:
        s = s.replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def detect_columns(header_rows: Sequence[Sequence[str]]) -> Dict[str, int]:
    """Определить роли колонок по тексту шапки. Порядок шаблонов важен: «с налогом» раньше «стоимость»."""
    roles: Dict[str, int] = {}
    text_rows = [[str(c or "").strip().lower() for c in row] for row in header_rows]
    width = max((len(r) for r in text_rows), default=0)
    for ci in range(width):
        cell = " ".join(r[ci] for r in text_rows if ci < len(r) and r[ci]).strip()
        if not cell:
            continue
        for role, patterns in COLUMN_PATTERNS:
            if role in roles:
                continue
            if any(re.search(p, cell) for p in patterns):
                roles[role] = ci
                break
    return roles


def _is_totals_row(row: Sequence[str]) -> bool:
    """Строка итогов: смотрим подпись (первую непустую ячейку), а не весь текст строки.

    Раньше проверялся текст всей строки с якорями ^итого$ — в строке «Итого | | 2 000,00» это не срабатывало,
    и итоги молча не проверялись.
    """
    label = next((str(c).strip().lower() for c in row if str(c).strip()), "")
    if any(re.search(p, label) for p in TOTALS_WORDS):
        return True
    joined = " ".join(str(c or "").lower() for c in row)
    return any(re.search(p.strip("^$"), joined) for p in (r"^итого$", r"^всего$")) and "итого" in joined


def check_table(rows: Sequence[Sequence[str]], columns: Optional[Dict[str, int]] = None,
                header_rows: int = 1) -> TableVerdict:
    """Проверить таблицу арифметикой. rows — строки таблицы, первая (или несколько) — шапка."""
    verdict = TableVerdict()
    if not rows:
        verdict.notes.append("пустая таблица")
        return verdict

    cols = columns or detect_columns(rows[:header_rows])
    verdict.columns = dict(cols)
    if not {"qty", "price", "amount"} <= set(cols):
        verdict.notes.append(f"не найдены колонки для проверки (нашли: {sorted(cols) or 'ничего'})")
        return verdict

    def cell(row: Sequence[str], role: str) -> Optional[float]:
        ci = cols.get(role)
        if ci is None or ci >= len(row):
            return None
        return parse_amount(row[ci])

    document_rows: List[int] = []
    for ri, row in enumerate(rows[header_rows:], start=header_rows):
        if not any(str(c).strip() for c in row):
            continue
        if _is_totals_row(row):
            document_rows.append(ri)
            continue
        qty, price, amount = cell(row, "qty"), cell(row, "price"), cell(row, "amount")
        if qty is None or price is None or amount is None:
            verdict.rows.append(RowCheck(ri, "no_data", ["нет количества/цены/стоимости"]))
            continue
        verdict.checked += 1
        details: List[str] = []
        expected = qty * price
        if abs(expected - amount) > max(AMOUNT_TOLERANCE, abs(expected) * 0.0005):
            details.append(f"кол-во × цена = {expected:.2f}, в таблице {amount:.2f}")
        tax, total, rate = cell(row, "tax"), cell(row, "total"), cell(row, "rate")
        if tax is not None:
            if total is not None and abs((amount + tax) - total) > max(AMOUNT_TOLERANCE, amount * 0.001):
                details.append(f"стоимость + налог = {amount + tax:.2f}, в таблице {total:.2f}")
            if rate:
                rate_share = rate / 100.0 if rate > 1 else rate
                for share, label in ((rate_share / (1 + rate_share), "в том числе"),
                                     (rate_share, "сверх")):
                    if abs(tax - amount * share) <= max(AMOUNT_TOLERANCE, amount * 0.005):
                        break
                else:
                    details.append(f"налог {tax:.2f} не сходится со ставкой {rate:g}%")
        if details:
            verdict.rows.append(RowCheck(ri, "mismatch", details))
        else:
            verdict.ok += 1
            verdict.rows.append(RowCheck(ri, "ok"))

    # Итоги: строка «Итого» сверяется с суммой строк ПО ТЕМ ЖЕ колонкам, что и строки (сумма — с суммой,
    # стоимость с налогом — со стоимостью с налогом). Иначе сравнение «яблок с апельсинами» даёт ложное расхождение.
    if document_rows:
        data_rows = [r for r in rows[header_rows:] if not _is_totals_row(r)]
        pairs = (("amount", "amount"), ("total", "total"))
        totals_ok: List[bool] = []
        for data_role, totals_role in pairs:
            dci, tci = cols.get(data_role), cols.get(totals_role)
            if dci is None or tci is None:
                continue
            values = [parse_amount(r[dci]) for r in data_rows if dci < len(r)]
            values = [v for v in values if v is not None]
            totals = [parse_amount(rows[ri][tci]) for ri in document_rows if tci < len(rows[ri])]
            totals = [t for t in totals if t is not None]
            if not values or not totals:
                continue
            verdict.totals_checked = True
            total_sum = sum(values)
            pair_ok = all(abs(total_sum - t) <= max(AMOUNT_TOLERANCE, total_sum * 0.001) for t in totals)
            totals_ok.append(pair_ok)
            if not pair_ok:
                verdict.notes.append(f"сумма строк {total_sum:.2f} против итога {totals[0]:.2f}")
        if totals_ok:
            verdict.totals_ok = all(totals_ok)
    return verdict


def check_recovered_table(table: Any) -> TableVerdict:
    """Проверить восстановленную таблицу (RecoveredTable): шапка — первая строка."""
    rows = getattr(table, "rows", None) or []
    return check_table(rows, header_rows=1)
