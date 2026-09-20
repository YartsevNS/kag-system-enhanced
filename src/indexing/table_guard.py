"""Защита от неверной цифры: проверки ПЕРЕД вычислением и ПОСЛЕ него.

Составлено по консультации с внешней моделью (reports/_scratch/consult_sql_part3.md).
Смысл модуля — не дать системе выдать правдоподобное, но неверное число. Порядок доверия:

1. **Кодом** (детерминированно): строки-итоги исключаются из агрегата, считается покрытие
   колонки числами, разбираются единицы измерения в значениях.
2. **Предупреждением**: посчитали, но с оговоркой (часть значений не числа, исключены
   строки-итоги, найдено несколько похожих строк).
3. **Отказом**: единицы в колонке разные, чисел в колонке почти нет, колонка для расчёта не
   найдена. Лучше честное «уточните», чем неверная сумма.

Готовые формулировки для пользователя взяты из консультации — они короткие и объясняют,
что именно надо уточнить.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

from src.indexing.table_store import parse_number

# Признаки строки-итога в наименовании. «Всего» и «Итого» в спецификациях — это сумма
# вышестоящих строк, и оставленная в агрегате она удваивает результат (классическая ошибка
# подсчёта по таблице).
TOTALS_MARKERS = ("итого", "всего", "итог", "total", "сумма по", "итого по", "всего по")

# Единицы, которые видим в значениях колонки. Если в одной колонке встречаются РАЗНЫЕ
# единицы (руб. и тыс. руб.), сумма без приведения бессмысленна.
UNIT_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"тыс\.?\s*руб", "тыс. руб."),
    (r"млн\.?\s*руб", "млн руб."),
    (r"руб", "руб."),
    (r"%", "%"),
    (r"шт", "шт."),
    (r"кг", "кг"),
    (r"т\b", "т"),
    (r"м2|м²", "м²"),
    (r"м3|м³", "м³"),
)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("ё", "е")


def find_total_rows(rows: Sequence[Dict[str, Any]], name_column: str | None) -> List[int]:
    """Номера строк, похожих на итоговые (по наименованию).

    Возвращаем номера, а не булево: их показываем пользователю («исключил строку Итого»),
    и по ним же чинит план запроса.
    """
    if not name_column:
        return []
    found: List[int] = []
    for row in rows:
        data = row.get("row_data") or {}
        value = _norm(data.get(name_column))
        if not value:
            continue
        # Проверяем начало значения: «Итого по разделу 3», «Всего:», «ИТОГО»
        if any(value.startswith(marker) for marker in TOTALS_MARKERS):
            found.append(int(row.get("row_index", -1)))
    return found


def numeric_coverage(rows: Sequence[Dict[str, Any]], column: str) -> Dict[str, Any]:
    """Сколько значений колонки распознано как числа, а сколько нет (и примеры «не чисел»).

    Нужно ДО агрегата: если половина значений — «—», «по запросу», «см. приложение»,
    сумма занижена и выдавать её как ответ нельзя.
    """
    parsed = 0
    missing_examples: List[str] = []
    missing = 0
    for row in rows:
        data = row.get("row_data") or {}
        raw = data.get(column)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        nums = row.get("row_num") or {}
        if column in nums or parse_number(text) is not None:
            parsed += 1
        else:
            missing += 1
            if len(missing_examples) < 3 and text not in missing_examples:
                missing_examples.append(text)
    total = parsed + missing
    return {
        "parsed": parsed,
        "missing": missing,
        "total": total,
        "share": round(parsed / total, 3) if total else 1.0,
        "examples": missing_examples,
    }


def detect_units(rows: Sequence[Dict[str, Any]], column: str) -> List[str]:
    """Какие единицы измерения встречаются в значениях колонки."""
    units: List[str] = []
    for row in rows:
        raw = (row.get("row_data") or {}).get(column)
        text = _norm(raw)
        if not text:
            continue
        for pattern, label in UNIT_PATTERNS:
            if re.search(pattern, text) and label not in units:
                units.append(label)
    return units


def pre_checks(rows: Sequence[Dict[str, Any]], column: str, name_column: str | None,
               aggregate: str) -> Dict[str, Any]:
    """Проверки перед выдачей числа: покрытие числами, единицы, строки-итоги.

    Возвращает {'refusals': [...], 'warnings': [...], 'total_rows': [...], 'units': [...]}.
    Отказ — не ошибка системы, а честное «нужно уточнение».
    """
    refusals: List[str] = []
    warnings: List[str] = []
    total_rows = find_total_rows(rows, name_column)
    coverage = numeric_coverage(rows, column)
    units = detect_units(rows, column)

    if aggregate in ("sum", "avg") and coverage["total"]:
        if coverage["share"] < 0.5:
            refusals.append(
                f"Не могу посчитать: в колонке «{column}» большинство значений не числа "
                f"({', '.join(coverage['examples'])}). Уточните, исключать их или считать нулём."
            )
        elif coverage["missing"]:
            warnings.append(
                f"{coverage['missing']} строк(и) со значением, которое не распознано как число "
                f"({', '.join(coverage['examples'])}) — итог может быть занижен"
            )

    if len(units) > 1:
        refusals.append(
            f"В колонке «{column}» разные единицы измерения ({', '.join(units)}) — "
            f"суммировать без приведения нельзя. Уточните единицу."
        )

    if total_rows:
        warnings.append(
            f"Строки-итоги («Итого/Всего», номера {total_rows}) исключены из расчёта, "
            f"чтобы не удвоить результат"
        )

    return {"refusals": refusals, "warnings": warnings,
            "total_rows": total_rows, "units": units, "coverage": coverage}


def post_checks(value: Any, rows: Sequence[Dict[str, Any]], column: str,
                name_column: str | None) -> List[str]:
    """Проверки после вычисления: сверка с контрольной строкой «Итого», аномалии."""
    warnings: List[str] = []
    total_rows = find_total_rows(rows, name_column)
    if total_rows and value is not None and isinstance(value, (int, float)):
        declared = None
        for row in rows:
            if int(row.get("row_index", -1)) in total_rows:
                declared = parse_number((row.get("row_data") or {}).get(column))
                if declared is not None:
                    break
        if declared is not None:
            diff = abs(float(value) - declared)
            if diff > max(1.0, 0.01 * abs(declared)):
                warnings.append(
                    f"Расчёт ({value}) не совпал со строкой «Итого» в документе ({declared}) — "
                    f"проверьте, что вопрос про всю таблицу"
                )
    if isinstance(value, (int, float)) and value < 0:
        warnings.append("Результат отрицательный — проверьте, так и должно быть")
    return warnings


def ambiguity_note(rows: Sequence[Dict[str, Any]], name_column: str | None) -> str:
    """Если фильтр по вхождению нашёл несколько РАЗНЫХ наименований — это надо показать.

    Иначе пользователь получит сумму по строкам, которые считал одной позицией.
    """
    if not name_column:
        return ""
    names: List[str] = []
    for row in rows:
        value = str((row.get("row_data") or {}).get(name_column) or "").strip()
        if value and value not in names:
            names.append(value)
    if len(names) > 1:
        return (f"По фильтру найдено несколько разных позиций: "
                f"{', '.join(names[:5])}{'…' if len(names) > 5 else ''}")
    return ""
