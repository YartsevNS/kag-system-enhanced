"""Таблицы: устойчивые идентификаторы и оценка качества извлечения.

Зачем отдельный модуль: и конвейер обработки (document_service), и чанкинг, и поиск
должны получать ОДИН И ТОТ ЖЕ идентификатор таблицы, а не каждый свой. Иначе строки
одной таблицы после переиндексации окажутся под разными `table_id`, и поиск по строкам
развалится на группы-однодневки.

`table_id` детерминированный: uuid5 от «документ + страница + номер таблицы на странице».
Переиндексация того же документа даёт те же id, а два документа с одинаковой таблицей —
разные (в отличие от хеша содержимого).

Оценка качества — из практики разбора табличных PDF (см. docs/guides/tables-plan.md):
одинаковая ширина строк, заполненность ячеек и признаки «это вообще таблица».
Качество сохраняется вместе с таблицей и решает, нужен ли второй экстрактор.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

# Пространство имён для идентификаторов таблиц. Константа зафиксирована навсегда:
# её изменение сделает несовместимыми уже сохранённые table_id.
TABLE_NAMESPACE = uuid.UUID("6f1c8f0e-6a2b-4f4e-9a3d-2b1c7e5d4a90")

# Порог «это вообще таблица»: ниже него результат извлечения считаем мусором
# (плоский текст, ошибочно принятый за таблицу).
MIN_FILL_RATIO = 0.15


def make_table_id(document_id: str, page_num: int, table_index: int) -> str:
    """Детерминированный id таблицы: одинаков для одного и того же места в документе."""
    key = f"{document_id}:{int(page_num)}:{int(table_index)}"
    return str(uuid.uuid5(TABLE_NAMESPACE, key))


def _as_text_rows(rows: Optional[List[List[Any]]]) -> List[List[str]]:
    """Привести 2D-массив ячеек к строкам без None (find_tables отдаёт None в пустых)."""
    out: List[List[str]] = []
    for row in rows or []:
        if row is None:
            continue
        out.append(["" if c is None else str(c) for c in row])
    return out


def table_stats(rows: Optional[List[List[Any]]]) -> Dict[str, Any]:
    """Простые измерения таблицы: размеры, заполненность, есть ли числа, шапка.

    Возвращает словарь — его же кладём в разметку и в document_tables, чтобы не
    пересчитывать при разборе.
    """
    text_rows = _as_text_rows(rows)
    if not text_rows:
        return {
            "row_count": 0, "col_count": 0, "fill_ratio": 0.0,
            "width_consistent": False, "has_numeric": False,
        }

    col_count = max(len(r) for r in text_rows)
    cells = [c for r in text_rows for c in r]
    non_empty = sum(1 for c in cells if c.strip())
    total_cells = len(text_rows) * col_count if col_count else 0
    fill_ratio = (non_empty / total_cells) if total_cells else 0.0
    width_consistent = all(len(r) == col_count for r in text_rows)

    def _is_numeric(value: str) -> bool:
        v = value.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        if not v:
            return False
        try:
            float(v.rstrip("%"))
            return True
        except ValueError:
            return False

    has_numeric = any(_is_numeric(c) for c in cells)

    return {
        "row_count": len(text_rows),
        "col_count": col_count,
        "fill_ratio": round(fill_ratio, 3),
        "width_consistent": width_consistent,
        "has_numeric": has_numeric,
    }


def table_quality(rows: Optional[List[List[Any]]]) -> float:
    """Оценка качества извлечения таблицы от 0 до 1.

    0.6 веса — одинаковая ширина строк (признак того, что границы колонок распознаны),
    0.4 — заполненность ячеек. Меньше двух строк или двух колонок — 0.0:
    это не таблица, а строка текста.
    """
    stats = table_stats(rows)
    if stats["row_count"] < 2 or stats["col_count"] < 2:
        return 0.0
    if stats["fill_ratio"] < MIN_FILL_RATIO:
        return 0.0

    width_part = 1.0 if stats["width_consistent"] else (
        sum(1 for r in _as_text_rows(rows) if len(r) == stats["col_count"]) / stats["row_count"]
    )
    return round(0.6 * width_part + 0.4 * stats["fill_ratio"], 3)


def tables_are_separate(segments: List[Dict[str, Any]]) -> int:
    """Сколько табличных сегментов среди сегментов парсера (для журнала обработки)."""
    return sum(
        1 for s in segments
        if isinstance(s, dict) and (s.get("metadata") or {}).get("chunk_type") == "table"
    )
