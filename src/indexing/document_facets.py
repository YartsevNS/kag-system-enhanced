"""Фасеты документа: закрытые перечни значений — ЕДИНСТВЕННЫЙ источник (пункт A3 дорожной карты).

Что такое фасет. Третья ось описания документа, независимая от вида (жанр) и темы (о чём):
фасет отвечает на узкий вопрос «какое свойство документа», и у каждого фасета свой ЗАКРЫТЫЙ
перечень значений. Фасетов у документа может не быть вовсе, а значений у одного фасета —
несколько (фасет многозначен).

Зачем закрытые перечни, а не свободный текст: пока список живёт в голове модели, одно свойство
называется по-разному в документах, фактах и выгрузках, и фильтры не складываются. Значение вне
перечня не пишется, а видно как отказ — тот же урок, что с типами связей в графе.

Что здесь ЕСТЬ (описано в словаре семантики, раздел facets):

* `protection_subject` (предмет защиты): данные, сети, люди, серверы, сервисы, не_применимо;
* `normative_force` (нормативная сила): обязательный, рекомендательный, справочный.

Оба размечены в пилоте JEV с уверенностью 0,83 — то есть это реалистичный материал для разметки
(в отличие от вида документа, который на фрагменте не определяется вовсе).

Чего здесь НЕТ намеренно: `stage` (этап), `status`, `classification_mark` (гриф). Они описаны в
черновике словаря, но разметкой ещё не проверялись, а «настройка, за которой нет кода» — дефект,
который мы уже вычищали. Добавлять их — вместе с разметкой и замером, а не заранее.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Версия словаря фасетов: по ней видно, какие документы размечены старым перечнем.
FACETS_VERSION = "v0"

# code → {title, values: {value: описание}}. Значения — коды латиницей? Нет: наборы короткие и
# языково-нейтральные по смыслу, коды русскими словами не годятся для движка (кириллица в ключах
# фильтров уже приносила проблемы при поиске). Поэтому код латиницей, подпись — по-русски.
FACETS: Dict[str, Dict] = {
    "protection_subject": {
        "title": "Предмет защиты",
        "definition": "На что направлены требования документа. Фасет многозначный.",
        "values": {
            "data": "данные, информация, персональные данные",
            "networks": "сети, каналы связи, телекоммуникации",
            "people": "сотрудники, персонал, физические лица",
            "servers": "серверы, оборудование, вычислительные средства",
            "services": "процессы, услуги, информационные системы",
            "not_applicable": "документ не про защиту",
        },
    },
    "normative_force": {
        "title": "Нормативная сила",
        "definition": "Обязательность документа для адресата. Фасет однозначный.",
        "values": {
            "mandatory": "содержит обязательные требования",
            "recommended": "рекомендации, которые можно не исполнять",
            "informational": "справочная или аналитическая информация без требований",
        },
    },
}

FACET_CODES: tuple = tuple(FACETS)


def is_valid_facet(code: Optional[str]) -> bool:
    return bool(code) and code in FACETS


def is_valid_value(facet: str, value: Optional[str]) -> bool:
    """Значение принадлежит ЗАКРЫТОМУ перечню своего фасета."""
    vals = (FACETS.get(facet) or {}).get("values") or {}
    return bool(value) and value in vals


def title(code: str) -> str:
    return (FACETS.get(code) or {}).get("title", code or "")


def value_title(facet: str, value: str) -> str:
    """Русская подпись значения; неизвестное показываем как есть (не выдумываем)."""
    return ((FACETS.get(facet) or {}).get("values") or {}).get(value, value)


def normalize(facets_in) -> Dict[str, List[str]]:
    """Привести фасеты к закрытым перечням.

    Возвращает {фасет: [значения]} — только известные фасеты и значения, дубли убраны, порядок
    значений берётся из словаря. Незнакомое отбрасывается: «значение вне перечня» и «фасет не
    определён» — разные вещи, смешивать значит потерять признак ошибки разметки.
    """
    out: Dict[str, List[str]] = {}
    if not isinstance(facets_in, dict):
        return out
    for facet, raw in facets_in.items():
        if not is_valid_facet(facet):
            continue
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        good = []
        for v in values:
            v = str(v or "").strip().lower()
            if is_valid_value(facet, v) and v not in good:
                good.append(v)
        if good:
            allowed = list((FACETS[facet].get("values") or {}))
            out[facet] = [v for v in allowed if v in good]
    return out


def as_list() -> List[Dict]:
    """Плоский список для интерфейса и API: [{code, title, definition, values:[{code,title}]}]."""
    out = []
    for code, meta in FACETS.items():
        out.append({
            "code": code,
            "title": meta.get("title", code),
            "definition": meta.get("definition", ""),
            "values": [{"code": v, "title": t} for v, t in (meta.get("values") or {}).items()],
        })
    return out


def vocabulary_line() -> str:
    """Строка фасетов и их значений для промпта: «protection_subject (data|networks|…), …»."""
    parts = []
    for code, meta in FACETS.items():
        vals = "|".join((meta.get("values") or {}).keys())
        parts.append(f"{code} ({vals})")
    return ", ".join(parts)
