"""Маршрутизатор табличных вопросов: вопрос → план запроса → ответ с ссылкой на источник.

Задача модуля — отвечать на ВЫЧИСЛИТЕЛЬНЫЕ вопросы («сколько позиций и на какую сумму»,
«что дороже 10 000», «сколько строк»). Сделано по итогам консультации с внешней моделью
(см. reports/_scratch/consult_sql*.md):

1. **Модель не пишет SQL.** Вопрос превращается в СТРУКТУРНЫЙ ПЛАН (таблица, колонки,
   операция, значение), а запрос собирает код. Свободный SQL на русских формулировках
   слишком часто путает «цена/стоимость/сумма» и выдумывает колонки.
2. **Канонические роли колонок.** «На какую сумму» надо уметь привязать к реальному
   «Цена, руб.»: заголовки нормализуются, сопоставляются со словарём синонимов и с
   формулировкой вопроса. Роль однозначна, имя колонки — как в документе.
3. **Арифметика в коде, а не в модели.** «На какую сумму» при наличии колонки суммы берётся
   как есть; если суммы нет — честно говорим, что посчитать нечем (умножение цены на
   количество делается отдельным шагом, см. TODO ниже).
4. **Четыре разных исхода**, которые нельзя смешивать: не понял вопрос / нет таких данных /
   по условию ничего не нашлось / посчитал, но часть значений не числа (сумма занижена).
   Пользователь должен видеть, какой это случай.

Плана пока достаточно, чтобы НЕ ошибаться на простых вопросах; сложные (несколько таблиц,
умножение колонок) сознательно отдаются обычному поиску — лучше честный ответ по тексту,
чем неверная цифра.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from src.indexing.table_store import parse_number, run_table_query, table_schema
from src.indexing.table_guard import ambiguity_note, post_checks, pre_checks

# ── канонические роли колонок и синонимы формулировок ───────────────────────
# Роль — то, ЧТО пользователь имеет в виду; синонимы — как это называют в документах
# и в вопросах. Список намеренно короткий и рабочий: добавлять по мере реальных промахов.
ROLE_SYNONYMS: Dict[str, List[str]] = {
    "name": ["наименование", "название", "позиция", "номенклатура", "объект", "услуга",
             "товар", "работа", "показатель", "реквизит"],
    "article": ["артикул", "код", "шифр", "обозначение", "номер по каталогу", "марка"],
    "quantity": ["количество", "кол-во", "колво", "шт", "штук", "объем", "объём", "масса",
                 "вес", "число единиц"],
    "price": ["цена", "прайс", "стоимость единицы", "цена за единицу", "расценка"],
    "amount": ["сумма", "итого", "всего", "стоимость", "стоимость всего", "затраты",
               "общая стоимость", "сумма с ндс", "сумма без ндс"],
    "unit": ["единица", "ед изм", "единица измерения", "ед. изм.", "единица измерения"],
    "section": ["раздел", "группа", "категория", "вид работ", "этап"],
    "date": ["дата", "срок", "период"],
    "norm": ["норма", "норматив", "расход", "норма расхода"],
}

# Слова-признаки вычисления в вопросе.
AGGREGATE_HINTS: Dict[str, Tuple[str, ...]] = {
    "sum": ("сколько всего", "на какую сумму", "сумма", "итого", "всего", "общая стоимость",
            "суммарно", "в сумме"),
    "count": ("сколько позиций", "сколько строк", "сколько наименований", "сколько записей",
              "сколько видов", "сколько пунктов", "число позиций", "количество позиций",
              "количество строк", "сколько всего позиций"),
    "max": ("максимальн", "самая дорогая", "самый дорогой", "наибольш", "максимум", "дороже всего"),
    "min": ("минимальн", "самая дешёвая", "самый дешёвый", "наименьш", "минимум", "дешевле всего"),
    "avg": ("средн", "в среднем", "средняя"),
}

# Отношения в вопросе → операция сравнения.
COMPARE_HINTS: Tuple[Tuple[str, str], ...] = (
    ("дороже", ">"), ("дешевле", "<"), ("свыше", ">"), ("более", ">"), ("больше", ">"),
    ("превышает", ">"), ("не менее", ">="), ("минимум", ">="), ("менее", "<"),
    ("меньше", "<"), ("до ", "<="), ("не более", "<="), ("равен", "="), ("равно", "="),
)

_NUMBER_IN_TEXT = re.compile(r"(\d[\d\s\u00a0]*(?:[.,]\d+)?)")


@dataclass
class TableCandidate:
    """Таблица-кандидат для ответа: её описание плюс оценка «насколько подходит»."""
    table_id: str
    document_id: str
    page_num: int
    columns: List[str]
    row_count: int
    roles: Dict[str, str] = field(default_factory=dict)
    score: float = 0.0
    reason: str = ""


def _normalize(text: str) -> str:
    """Нормализация заголовка/фразы: нижний регистр, без единиц измерения и пунктуации."""
    value = (text or "").lower().replace("ё", "е")
    value = re.sub(r"\(.*?\)", " ", value)
    value = re.sub(r"[,.]?\s*(руб|р\.|тыс|млн|шт|м|мм|кг|т|чел|дн|ч|%)\b\.?", " ", value)
    value = re.sub(r"[^a-zа-я0-9\s\-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def map_columns(columns: Sequence[str], question: str = "") -> Dict[str, str]:
    """Сопоставить колонки таблицы с каноническими ролями.

    Порядок: точное совпадение с синонимом → вхождение синонима в заголовок → вхождение
    нормализованного заголовка в вопрос. Роль занимается один раз (первая подходящая
    колонка выигрывает): две колонки одной роли в плане бессмысленны.
    """
    roles: Dict[str, str] = {}
    norm_question = _normalize(question)

    def _claim(role: str, column: str) -> bool:
        if role in roles:
            return False
        roles[role] = column
        return True

    normalized = {col: _normalize(col) for col in columns}

    # 1. точное совпадение заголовка с синонимом
    for role, synonyms in ROLE_SYNONYMS.items():
        for col, norm_col in normalized.items():
            if norm_col and norm_col in synonyms:
                _claim(role, col)

    # 2. вхождение синонима в заголовок («цена за единицу, руб.» → price)
    for role, synonyms in ROLE_SYNONYMS.items():
        if role in roles:
            continue
        for col, norm_col in normalized.items():
            if not norm_col:
                continue
            if any(syn in norm_col for syn in synonyms):
                _claim(role, col)

    # 3. вхождение заголовка в вопрос (пользователь назвал колонку своими словами)
    if norm_question:
        for role, synonyms in ROLE_SYNONYMS.items():
            if role in roles:
                continue
            for col, norm_col in normalized.items():
                if norm_col and norm_col in norm_question:
                    _claim(role, col)
    return roles


def detect_aggregate(question: str, roles: Dict[str, str]) -> Optional[str]:
    """Понять, просят ли вычисление, и какое именно.

    Порядок проверки важен: «сколько всего позиций на какую сумму» — это сумма, а не
    количество, поэтому sum-признаки проверяются раньше count.

    Подсказки нормализуются ТЕМ ЖЕ правилом, что и вопрос: иначе «самая дешёвая» с «ё»
    не находит себя в нормализованном вопросе (живой промах, поймано тестом).
    """
    q = _normalize(question)
    for agg in ("sum", "max", "min", "avg"):
        if any(_normalize(hint) in q for hint in AGGREGATE_HINTS[agg]):
            return agg
    if any(_normalize(hint) in q for hint in AGGREGATE_HINTS["count"]):
        return "count"
    if q.startswith("сколько") and "позиц" in q:
        return "count"
    return None


def detect_filters(question: str, roles: Dict[str, str]) -> List[Dict[str, Any]]:
    """Собрать фильтры: сравнение с числом и/или вхождение названия.

    Правила (зашиты в код, а не отданы модели):
    - «дороже/дешевле/более/менее <число>» → сравнение по роли price, если она есть,
      иначе по quantity;
    - «<Число> рублей» тоже сравнение, если вопрос про цену;
    - слово с цифрами и дефисом (НЦ-50, АБВ-123) или слово с заглавной буквы внутри
      вопроса → вхождение по роли name/article.
    """
    q = _normalize(question)
    filters: List[Dict[str, Any]] = []

    compare_role = "price" if "price" in roles else ("quantity" if "quantity" in roles else None)
    op = None
    for hint, candidate_op in COMPARE_HINTS:
        if _normalize(hint) in q:
            op = candidate_op
            break
    if op and compare_role:
        numbers = _NUMBER_IN_TEXT.findall(q.replace("\u00a0", " "))
        # Последнее число в фразе — обычно то, с которым сравнивают («позиции дороже 10 000»)
        if numbers:
            value = parse_number(numbers[-1])
            if value is not None:
                filters.append({"column": roles[compare_role], "op": op, "value": value})

    # Вхождение названия/артикула: латиница+цифры+дефис или слово, начинавшееся с заглавной
    tokens = re.findall(r"[А-ЯA-Z][\w\-]{1,}", question or "")
    for token in tokens:
        if len(token) < 3:
            continue
        low = token.lower()
        if low in ("сколько", "какая", "какой", "какие", "сумма", "итого", "всего", "таблице",
                   "таблица", "позиции", "позиция", "цена", "стоимость", "строк", "строки"):
            continue
        role = "article" if "article" in roles and re.search(r"\d", token) else None
        role = role or ("name" if "name" in roles else ("article" if "article" in roles else None))
        if role:
            filters.append({"column": roles[role], "op": "contains", "value": token})
            break
    return filters


def pick_table(question: str, candidates: Sequence[Dict[str, Any]],
               prefer_documents: Optional[Sequence[str]] = None) -> Optional[TableCandidate]:
    """Выбрать таблицу-кандидата: покрытие ролей + совпадение по документам поиска.

    Оценка простая и объяснимая: сколько нужных ролей закрывает таблица (её колонки),
    есть ли данные в строках под запрошенное название и относится ли она к документам,
    которые нашёл векторный поиск. Меньше ролей — ниже оценка.
    """
    prefer = set(prefer_documents or [])
    best: Optional[TableCandidate] = None

    for entry in candidates:
        columns = entry.get("columns") or []
        if not columns:
            continue
        roles = map_columns(columns, question)
        score = 0.0
        reasons: List[str] = []

        for role in ("name", "article", "price", "quantity", "amount", "section"):
            if role in roles:
                score += 1.0

        if prefer and entry.get("document_id") in prefer:
            score += 2.0
            reasons.append("документ найден поиском")

        score += min(entry.get("row_count", 0), 100) / 100.0  # мелкая премия за наполненность

        candidate = TableCandidate(
            table_id=entry["table_id"],
            document_id=entry.get("document_id", ""),
            page_num=entry.get("page_num", 0),
            columns=list(columns),
            row_count=entry.get("row_count", 0),
            roles=roles,
            score=round(score, 3),
            reason=", ".join(reasons),
        )
        if best is None or candidate.score > best.score:
            best = candidate
    return best


def build_plan(question: str, candidate: TableCandidate) -> Dict[str, Any]:
    """План запроса по вопросу и выбранной таблице (без SQL — его соберёт table_store)."""
    aggregate = detect_aggregate(question, candidate.roles)
    filters = detect_filters(question, candidate.roles)

    plan: Dict[str, Any] = {"table_id": candidate.table_id, "filters": filters}

    if aggregate == "count":
        plan["aggregate"] = "count"
        return plan

    if aggregate in ("sum", "avg", "max", "min"):
        # Считаем по подходящей роли: сумма — по amount (если есть) или по price,
        # количество — по quantity. Роль выбирается кодом, не моделью.
        role_order = {
            "sum": ("amount", "price"),
            "avg": ("price", "amount"),
            "max": ("price", "amount"),
            "min": ("price", "amount"),
        }[aggregate]
        column = next((candidate.roles[r] for r in role_order if r in candidate.roles), None)
        if column is None:
            plan["error"] = (
                f"в таблице нет колонки для «{aggregate}» "
                f"(есть колонки: {', '.join(candidate.columns[:6])})"
            )
            return plan
        plan["aggregate"] = aggregate
        plan["aggregate_column"] = column
        if "name" in candidate.roles and filters:
            plan["group_by"] = None
        return plan

    # Вычисления не просили: вопрос про конкретные строки — отдаём фильтры, а решает
    # обычный поиск (здесь SQL нужен только чтобы показать найденное).
    plan["filters"] = filters
    if filters:
        plan["order_by"] = candidate.roles.get("price")
    return plan


def answer_from_tables(question: str, document_ids: Optional[Sequence[str]] = None,
                       sources: Optional[Sequence[Dict[str, Any]]] = None,
                       max_tables: int = 40) -> Dict[str, Any]:
    """Главная точка входа: ответить на вопрос по таблицам или сказать, почему нельзя.

    Возвращает словарь со статусом:
      ok        — посчитали; есть текст ответа, план и ссылка на источник;
      no_rows   — по условию ничего не нашлось (это ОТВЕТ, а не ошибка);
      no_data   — нет таблиц/колонки, по которой можно считать;
      unparsed  — вычисление просят, но план не построился (вопрос не про эти таблицы).
    """
    schema = table_schema()
    if not schema:
        return {"status": "no_data", "message": "в строчном слое пока нет таблиц",
                "reason": "нет данных"}

    if document_ids:
        allowed = set(document_ids)
        schema = [t for t in schema if t.get("document_id") in allowed] or schema

    candidates = schema[:max_tables]
    candidate = pick_table(question, candidates, prefer_documents=document_ids)
    if candidate is None:
        return {"status": "no_data", "message": "подходящих таблиц не найдено", "reason": "нет данных"}

    plan = build_plan(question, candidate)
    if plan.get("error"):
        return {"status": "no_data", "message": plan["error"], "reason": "нет данных",
                "table": candidate.__dict__}

    aggregate = plan.get("aggregate")
    filters = plan.get("filters") or []
    if not aggregate and not filters:
        # Ни вычисления, ни условия: это обычный смысловой вопрос, SQL ему не нужен.
        return {"status": "unparsed", "message": "вопрос не про вычисление по таблице",
                "reason": "не понял вопрос", "table": candidate.__dict__}

    name_column = candidate.roles.get("name")
    target_column = plan.get("aggregate_column") or (filters[0]["column"] if filters else None)

    # ── Проверки ПЕРЕД вычислением ──────────────────────────────────────────
    # Сначала смотрим на сами данные (сколько значений реально числа, какие единицы,
    # есть ли строки-итоги), и только потом считаем. Отказ здесь — это честное
    # «уточните», а не сбой: неверная сумма хуже отсутствия ответа.
    probe = run_table_query({**plan, "aggregate": None, "aggregate_column": None, "group_by": None})
    probe_rows = probe.get("rows") or []

    if aggregate in ("sum", "avg", "min", "max") and target_column:
        checks = pre_checks(probe_rows, target_column, name_column, aggregate)
        if checks["refusals"]:
            return {
                "status": "clarify",
                "message": " ".join(checks["refusals"]),
                "reason": "нужно уточнение",
                "plan": plan,
                "table": candidate.__dict__,
            }
        guard_warnings = list(checks["warnings"])
        # Строки-итоги исключаем из самого запроса, а не «на словах».
        if checks["total_rows"] and name_column:
            for marker in ("Итого", "Всего"):
                plan.setdefault("filters", []).append(
                    {"column": name_column, "op": "not_contains", "value": marker})
    else:
        guard_warnings = []

    result = run_table_query(plan)
    if result.get("error"):
        return {"status": "unparsed", "message": result["error"], "reason": "не понял вопрос",
                "plan": plan}

    # Проверка «а ту ли колонку выбрали»: значение вхождения (например «НЦ-50») может
    # лежать не в артикуле, а в наименовании. Вместо догадки — перебираем текстовые
    # колонки и берём ту, где данные реально есть. Догадка здесь стоила бы пустой
    # выдачи там, где ответ есть.
    def _is_empty(res: Dict[str, Any]) -> bool:
        """Пусто для нас — это и «нет строк», и «агрегат посчитал по нулю строк».

        Второй случай обязателен: SUM по пустому множеству возвращает ОДНУ строку со
        значением NULL, поэтому проверка «нет строк» его не видит (поймано тестом).
        """
        rows_ = res.get("rows") or []
        if not rows_:
            return True
        if aggregate and all(r.get("value") is None for r in rows_):
            return True
        return False

    note = ""
    contains = [f for f in (plan.get("filters") or []) if f.get("op") == "contains"]
    if contains and _is_empty(result):
        numeric_roles = {"price", "quantity", "amount", "norm", "date"}
        alt_columns = [
            col for col in candidate.columns
            if col != contains[0]["column"]
            and candidate.roles.get(
                next((r for r, c in candidate.roles.items() if c == col), ""), "") not in numeric_roles
        ]
        for alt in alt_columns:
            alt_plan = dict(plan)
            alt_plan["filters"] = [
                ({**f, "column": alt} if f.get("op") == "contains" else f)
                for f in plan["filters"]
            ]
            alt_result = run_table_query(alt_plan)
            if not _is_empty(alt_result):
                result = alt_result
                plan = alt_plan
                note = f"значение найдено в колонке «{alt}»"
                logger.info(f"[tables] значение вхождения найдено в другой колонке: {alt}")
                break

    rows = result.get("rows") or []
    source_ref = (f"документ {candidate.document_id[:8]}, стр. {candidate.page_num}, "
                  f"таблица {candidate.table_id[:8]}")

    # Пустой результат различаем аккуратно: агрегат может вернуть строку со значением None
    # (например sum по колонке, где ни одно значение не распозналось) — это тоже «нет данных».
    is_empty = (not rows) or bool(aggregate and all(r.get("value") is None for r in rows))
    if is_empty:
        return {
            "status": "no_rows",
            "message": "по этому условию в таблице ничего не найдено",
            "reason": "ноль по фильтру",
            "plan": plan,
            "source": source_ref,
        }

    # Текст ответа и предупреждения о качестве цифры
    lines: List[str] = []
    if aggregate == "count":
        lines.append(f"Всего строк по условию: {int(rows[0]['value'] or 0)}")
    elif aggregate:
        agg_col = plan.get("aggregate_column")
        for row in rows[:20]:
            if row.get("group_key") is not None:
                lines.append(f"{row['group_key']}: {row.get('value')}")
            else:
                lines.append(f"{aggregate}({agg_col}) = {row.get('value')} "
                             f"(строк в расчёте: {row.get('rows_in_group')})")
    else:
        for row in rows[:20]:
            data = row.get("row_data") or {}
            if data:
                lines.append("; ".join(f"{k}: {v}" for k, v in list(data.items())[:8]))

    warnings: List[str] = list(guard_warnings)
    if aggregate in ("sum", "avg") and plan.get("aggregate_column") and rows:
        # После расчёта: сверка с контрольной строкой «Итого» (если она есть в документе)
        # и проверка аномалий (например, отрицательный итог).
        first_value = None if plan.get("group_by") else rows[0].get("value")
        warnings.extend(post_checks(first_value, probe_rows, plan["aggregate_column"], name_column))
        ambiguous = ambiguity_note(probe_rows, name_column)
        if ambiguous:
            note = (note + "; " if note else "") + ambiguous

    return {
        "status": "ok",
        "answer": "\n".join(lines),
        "warning": "; ".join(warnings),
        "note": note,
        "plan": plan,
        "rows": rows[:20],
        "row_count": result.get("row_count"),
        "truncated": result.get("truncated"),
        "source": source_ref,
        "table": candidate.__dict__,
        "reason": "посчитано",
    }


def context_block(result: Dict[str, Any]) -> str:
    """Блок контекста для модели: цифры посчитаны SQL, и это прямо сказано."""
    if result.get("status") == "clarify":
        # Отказ считать — это тоже ответ. Модель не должна вместо уточнения выдумывать число.
        return ("\n\n--- ТАБЛИЦЫ (SQL) ---\n"
                f"Точный расчёт невозможен: {result.get('message')}\n"
                "Так и ответь — задай уточняющий вопрос. Число не называй.")
    if result.get("status") == "no_rows":
        return ("\n\n--- ТАБЛИЦЫ (SQL) ---\n"
                f"По условию вопроса в таблице ничего не найдено ({result.get('source', '')}). "
                "Так и ответь: данных по этому условию нет.")
    if result.get("status") != "ok":
        return ""
    head = ("\n\n--- ТАБЛИЦЫ (значения посчитаны SQL по строкам, а не взяты из текста) ---\n"
            f"Источник: {result.get('source')}\n{result.get('answer')}")
    if result.get("warning"):
        head += f"\nВНИМАНИЕ: {result['warning']}"
    head += ("\nВ ответе приведи это значение и сошлись на документ и страницу. "
             "Не пересчитывай сам и не округляй по-своему.")
    return head
