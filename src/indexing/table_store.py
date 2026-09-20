"""Строчный слой таблиц: сохранение строк, чтение, безопасные точные запросы.

Два принципа, на которых держится этот модуль:

1. **Поиск и вычисление — разные вещи.** «В каких строках упоминается насос НЦ-50» —
   это поиск (его делает векторный слой). «Сколько всего насосов и на какую сумму» —
   это вычисление: оно должно идти SQL-ом, а не подсчётом по найденным кускам текста,
   иначе цифра в ответе зависит от того, какие фрагменты попали в контекст.

2. **Модель не пишет свободный SQL.** План запроса (какие колонки, какая операция,
   какое значение) строится нами и проверяется: колонка обязана существовать в самой
   таблице, операция — из белого списка, значение всегда уходит параметром. Имя колонки
   попадает в SQL только после проверки по заголовкам из базы, значение — никогда.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence

from loguru import logger

# Белый список операций: только сравнения и подстрока. Никаких функций, LIKE с шаблоном
# от пользователя, подзапросов и UNION — план приходит из модели, а не от человека.
ALLOWED_OPS = ("=", "!=", ">", ">=", "<", "<=", "contains")

# Агрегаты для вычислений.
ALLOWED_AGGREGATES = ("sum", "avg", "min", "max", "count")

_NUM_CLEAN = re.compile(r"[^\d,.\-]")


def parse_number(value: Any) -> Optional[float]:
    """Распознать число из ячейки: «1 234,56» → 1234.56, «12%» → 12.0, «—» → None.

    Оригинал при этом НЕ теряется: он остаётся в row_data. Здесь только распознанная
    копия — по ней считаем суммы и сравниваем; если распознать не удалось, None,
    и строка не участвует в вычислениях (лучше пропустить, чем посчитать мусор).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    cleaned = _NUM_CLEAN.sub("", text).replace("\u00a0", "")
    if not cleaned or cleaned in {"-", ".", ","}:
        return None
    # Разделитель тысяч — пробел; десятичный — запятая. Если и точка, и запятая есть,
    # запятая считается десятичной («1.234,56»), иначе точка десятичная («1234.56»).
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return None


def build_row_text(headers: Sequence[str], row: Sequence[Any]) -> str:
    """Представление строки «Ключ: значение; …» — для поиска и для контекста модели.

    Не CSV: пары «ключ: значение» модель читает точнее, и по ним видно, к какой колонке
    относится значение (в CSV колонки путаются, если в ячейке есть запятая).
    """
    pairs: List[str] = []
    for idx, value in enumerate(row):
        cell = "" if value is None else str(value).strip()
        if not cell:
            continue
        key = str(headers[idx]).strip() if idx < len(headers) else f"колонка_{idx}"
        pairs.append(f"{key}: {cell}" if key else cell)
    return "; ".join(pairs)


def normalize_headers(headers: Optional[Sequence[Any]]) -> List[str]:
    """Заголовки без пустых имён: пустая шапка — не повод потерять колонку.

    Колонка получает имя «колонка_N», иначе строка превратилась бы в «: 12», и по ней
    нельзя ни искать, ни считать.
    """
    out: List[str] = []
    for idx, raw in enumerate(headers or []):
        name = str(raw or "").strip()
        out.append(name or f"колонка_{idx}")
    return out


def rows_to_records(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Строки таблицы → записи для сохранения: оригинал, числа, текст."""
    keys = normalize_headers(headers)
    records: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        values = list(row or [])
        data = {keys[i] if i < len(keys) else f"колонка_{i}": (
            "" if values[i] is None else str(values[i])
        ) for i in range(len(values))}
        nums = {}
        for key, value in data.items():
            num = parse_number(value)
            if num is not None:
                nums[key] = num
        records.append({
            "row_index": idx,
            "row_data": data,
            "row_num": nums,
            "row_text": build_row_text(keys, values),
        })
    return records


# ── сохранение ──────────────────────────────────────────────────────────────

def save_table_rows(document_id: str, table_id: str, headers: Sequence[str],
                    rows: Sequence[Sequence[Any]], page_num: int = 0,
                    session=None) -> int:
    """Записать строки таблицы (перезаписывая прежние — идемпотентно при переиндексации).

    Возвращает число записанных строк. Ошибка записи не должна валить обработку
    документа: строки — вспомогательный слой, таблица целиком уже сохранена.
    """
    records = rows_to_records(headers, rows)
    if not records:
        return 0
    own_session = session is None
    try:
        from src.database.session import get_session_local
        from src.database.table_row_models import TableRow

        maker = get_session_local()
        session = session or maker()
        try:
            session.query(TableRow).filter_by(table_id=table_id).delete()
            for rec in records:
                session.add(TableRow(
                    document_id=document_id,
                    table_id=table_id,
                    row_index=rec["row_index"],
                    page_num=page_num,
                    row_data=rec["row_data"],
                    row_num=rec["row_num"],
                    row_text=rec["row_text"],
                ))
            session.commit()
        finally:
            if own_session:
                session.close()
        return len(records)
    except Exception as e:
        logger.warning(f"[tables] строки таблицы {table_id[:8]} не сохранены: {e}")
        return 0


# ── чтение и описание ───────────────────────────────────────────────────────

def table_schema(document_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Описание таблиц: какая таблица, на какой странице, какие колонки, сколько строк.

    Это «каталог» для маршрутизатора: чтобы построить план запроса, модели нужно знать
    доступные таблицы и их колонки, а не весь текст документа.
    """
    from src.database.session import get_session_local
    from src.database.table_row_models import TableRow

    maker = get_session_local()
    session = maker()
    try:
        q = session.query(TableRow)
        if document_id:
            q = q.filter(TableRow.document_id == document_id)
        out: Dict[str, Dict[str, Any]] = {}
        for row in q.all():
            entry = out.setdefault(row.table_id, {
                "table_id": row.table_id,
                "document_id": row.document_id,
                "page_num": row.page_num,
                "columns": [],
                "row_count": 0,
            })
            entry["row_count"] += 1
            for key in (row.row_data or {}):
                if key not in entry["columns"]:
                    entry["columns"].append(key)
        return list(out.values())
    finally:
        session.close()


def rows_for_table(table_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """Строки таблицы по её table_id (для показа, контекста и проверок)."""
    from src.database.session import get_session_local
    from src.database.table_row_models import TableRow

    maker = get_session_local()
    session = maker()
    try:
        rows = (session.query(TableRow)
                .filter(TableRow.table_id == table_id)
                .order_by(TableRow.row_index)
                .limit(max(1, int(limit)))
                .all())
        return [r.to_dict() for r in rows]
    finally:
        session.close()


# ── построение безопасного запроса по плану ─────────────────────────────────

def _json_text_expr(dialect: str, column: str) -> str:
    """Выражение «значение колонки как текст» для текущей СУБД.

    Имя колонки попадает в SQL только после проверки по ЗАГОЛОВКАМ ТАБЛИЦЫ (см.
    validate_plan), это не пользовательский ввод.
    """
    quoted = column.replace("'", "''")
    if dialect == "postgresql":
        return f"row_data ->> '{quoted}'"
    # SQLite (тесты): json_extract по JSON-колонке (её SQLAlchemy хранит как TEXT)
    return f"json_extract(row_data, '$.{quoted}')"


def _json_num_expr(dialect: str, column: str) -> str:
    quoted = column.replace("'", "''")
    if dialect == "postgresql":
        return f"(row_num ->> '{quoted}')::numeric"
    return f"json_extract(row_num, '$.{quoted}')"


def validate_plan(plan: Dict[str, Any], available_columns: Sequence[str]) -> Dict[str, Any]:
    """Проверить план запроса: таблица известна, колонки существуют, операции допустимы.

    Ничего не «исправляем» молча: непонятный план лучше отклонить и уйти на обычный
    поиск, чем построить запрос наугад и отдать пользователю неверную цифру.
    """
    if not isinstance(plan, dict):
        raise ValueError("план запроса должен быть словарём")

    columns = set(available_columns or [])
    if not columns:
        raise ValueError("у таблицы не найдено колонок")

    filters = []
    for f in plan.get("filters") or []:
        if not isinstance(f, dict):
            raise ValueError("фильтр должен быть словарём")
        column = str(f.get("column") or "")
        op = str(f.get("op") or "=")
        if column not in columns:
            raise ValueError(f"колонки {column!r} нет в таблице")
        if op not in ALLOWED_OPS:
            raise ValueError(f"операция {op!r} не разрешена")
        value = f.get("value")
        if value is None or str(value).strip() == "":
            raise ValueError("пустое значение фильтра")
        filters.append({"column": column, "op": op, "value": value})

    aggregate = plan.get("aggregate")
    if aggregate is not None:
        aggregate = str(aggregate).lower()
        if aggregate not in ALLOWED_AGGREGATES:
            raise ValueError(f"агрегат {aggregate!r} не разрешён")
        if aggregate != "count":
            agg_col = str(plan.get("aggregate_column") or "")
            if agg_col not in columns:
                raise ValueError(f"колонка агрегата {agg_col!r} нет в таблице")

    group_by = plan.get("group_by")
    if group_by is not None and group_by not in columns:
        raise ValueError(f"колонки группировки {group_by!r} нет в таблице")

    return {
        "table_id": str(plan.get("table_id") or ""),
        "filters": filters,
        "aggregate": aggregate,
        "aggregate_column": plan.get("aggregate_column"),
        "group_by": group_by,
        "order_by": plan.get("order_by"),
    }


def run_table_query(plan: Dict[str, Any], max_rows: int = 500,
                    timeout_ms: int = 5000) -> Dict[str, Any]:
    """Выполнить проверенный план: фильтры, суммы, группировки.

    Возвращает {columns, rows, row_count, truncated, sql_note}. Значения фильтров уходят
    ПАРАМЕТРАМИ; имена колонок — только после проверки по заголовкам таблицы.
    """
    from sqlalchemy import text as sql_text

    from src.database.session import get_session_local

    def _fail(msg: str) -> Dict[str, Any]:
        logger.info(f"[tables] запрос отклонён: {msg}")
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False, "error": msg}

    table_id = str(plan.get("table_id") or "")
    if not table_id:
        return _fail("не указана таблица")

    schema = {t["table_id"]: t for t in table_schema()}
    entry = schema.get(table_id)
    if not entry:
        return _fail(f"таблица {table_id[:8]} не найдена в строчном слое")

    try:
        checked = validate_plan(plan, entry["columns"])
    except ValueError as e:
        return _fail(str(e))

    maker = get_session_local()
    session = maker()
    try:
        dialect = session.bind.dialect.name
        params: Dict[str, Any] = {"lim": int(max_rows) + 1}
        where_parts: List[str] = ["table_id = :table_id"]
        params["table_id"] = table_id

        for i, f in enumerate(checked["filters"]):
            op = f["op"]
            value = f["value"]
            key = f"v{i}"
            if op == "contains":
                where_parts.append(f"{_json_text_expr(dialect, f['column'])} LIKE :{key}")
                params[key] = f"%{value}%"
                continue
            num = parse_number(value)
            if num is not None:
                where_parts.append(f"{_json_num_expr(dialect, f['column'])} {op} :{key}")
                params[key] = num
            else:
                if op not in ("=", "!="):
                    return _fail(f"значение {value!r} не число — сравнение {op} невозможно")
                where_parts.append(f"{_json_text_expr(dialect, f['column'])} {op} :{key}")
                params[key] = str(value)

        where_sql = " AND ".join(where_parts)

        aggregate = checked["aggregate"]
        agg_col = checked["aggregate_column"]
        group_by = checked["group_by"]

        # Собираем SELECT по одному из четырёх случаев — без «доработок» после сборки,
        # иначе легко получить агрегат, применённый дважды или потерянный.
        if not aggregate:
            select_parts = ["row_index", "row_data", "row_num"]
            group_sql = ""
        elif aggregate == "count" and not group_by:
            select_parts = ["COUNT(*) AS value"]
            group_sql = ""
        elif aggregate == "count":
            select_parts = [f"{_json_text_expr(dialect, group_by)} AS group_key", "COUNT(*) AS value"]
            group_sql = f" GROUP BY {_json_text_expr(dialect, group_by)}"
        elif not group_by:
            select_parts = [
                f"{aggregate.upper()}({_json_num_expr(dialect, agg_col)}) AS value",
                "COUNT(*) AS rows_in_group",
            ]
            group_sql = ""
        else:
            select_parts = [
                f"{_json_text_expr(dialect, group_by)} AS group_key",
                f"{aggregate.upper()}({_json_num_expr(dialect, agg_col)}) AS value",
                "COUNT(*) AS rows_in_group",
            ]
            group_sql = f" GROUP BY {_json_text_expr(dialect, group_by)}"

        order_sql = ""
        if checked["order_by"] and checked["order_by"] in entry["columns"] and not aggregate:
            order_sql = f" ORDER BY {_json_num_expr(dialect, checked['order_by'])} DESC"

        sql = f"SELECT {', '.join(select_parts)} FROM table_rows WHERE {where_sql}{group_sql}{order_sql} LIMIT :lim"

        import time
        started = time.monotonic()
        result = session.execute(sql_text(sql), params)
        rows = result.fetchall()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if elapsed_ms > int(timeout_ms):
            logger.warning(f"[tables] запрос выполнялся {elapsed_ms} мс (лимит {timeout_ms})")

        columns = list(result.keys())
        truncated = len(rows) > int(max_rows)
        payload = [dict(zip(columns, r)) for r in rows[:int(max_rows)]]
        return {
            "columns": columns,
            "rows": payload,
            "row_count": len(payload),
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
            "aggregate": aggregate,
            "table_id": table_id,
            "document_id": entry["document_id"],
            "page_num": entry["page_num"],
        }
    except Exception as e:
        logger.warning(f"[tables] запрос не выполнен: {e}")
        return _fail(f"ошибка выполнения: {e}")
    finally:
        session.close()
