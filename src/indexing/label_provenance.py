"""Происхождение разметки документа: кто и как поставил значение.

Зачем отдельный слой. Значение в базе (тема, фасет, вид) может быть поставлено моделью, правилом
или человеком. Без явной пометки через месяц машинный ответ неотличим от правки человека, а разница
принципиальная: машинное значение можно перезаписать следующим прогоном модели, ручное — нельзя,
иначе правка живого специалиста молча исчезает при переразметке.

Форма записи (JSON в `documents.label_provenance`):

    {
      "rubrics": {"source": "model", "value": ["banking"], "confidence": 0.53, "flagged": true,
                  "alternatives": [["banking", 0.53], ["economics", 0.57]]},
      "facets.normative_force": {"source": "manual", "by": "ivanov", "at": "2026-10-10T12:00:00",
                                 "note": "методические рекомендации, не обязательные требования"}
    }

Правила:
  * `source`: `model` — вывод модели (можно перезаписать), `manual` — правка человека;
  * `flagged`: значение ниже порога уверенности — его показывают на странице разбора;
  * `manual` НЕ перезаписывается ни моделью, ни прибором: снять такую пометку может только
    администратор (проверка — в API, а не здесь: этот модуль только хранит форму);
  * полей может быть больше, чем одно на документ: у темы (список) и у каждого фасета — своя запись.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Порог, ниже которого машинное значение считается спорным и показывается человеку.
# Обоснование: на корпусе документов значения с уверенностью ниже 0,6 давали разнобой в повторах
# (одна и та же формулировка отвечала по-разному), а выше 0,9 — совпадали всегда. Порог меняется
# только замером, а не «на глаз».
FLAG_BELOW = 0.6

FIELD_RUBRICS = "rubrics"
FIELD_TYPE = "document_type"


def facet_field(facet: str) -> str:
    """Имя поля происхождения для фасета: `facets.<код>` (у каждого фасета своя запись)."""
    return f"facets.{facet}"


def field_title(field: str) -> str:
    """Человеческая подпись поля для интерфейса и журнала."""
    from src.indexing import document_facets

    if field == FIELD_RUBRICS:
        return "Тема"
    if field == FIELD_TYPE:
        return "Вид документа"
    if field.startswith("facets."):
        return document_facets.title(field.split(".", 1)[1])
    return field


def parse(raw: Any) -> Dict[str, dict]:
    """Разобрать значение колонки (JSON-строка или уже словарь) в словарь происхождения."""
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def dump(prov: Dict[str, dict]) -> str:
    return json.dumps(prov or {}, ensure_ascii=False)


def get(prov: Dict[str, dict], field: str) -> dict:
    return (prov or {}).get(field) or {}


def is_manual(prov: Dict[str, dict], field: str) -> bool:
    """Значение правил человек — модель и приборы его не перезаписывают."""
    return get(prov, field).get("source") == "manual"


def manual_fields(prov: Dict[str, dict]) -> List[str]:
    return [f for f, meta in (prov or {}).items() if meta.get("source") == "manual"]


def mark_model(prov: Dict[str, dict], field: str, value: Any, confidence: Optional[float],
               alternatives: Optional[List[List[Any]]] = None) -> Dict[str, dict]:
    """Записать происхождение машинного значения.

    Ручную пометку не трогаем: если поле правил человек, модель туда не пишет вовсе (иначе
    уверенность модели затрёт след правки, и на странице разбора поле снова покажется спорным).
    """
    out = dict(prov or {})
    if is_manual(out, field):
        return out
    try:
        conf = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        conf = None
    entry: Dict[str, Any] = {"source": "model", "value": value}
    if conf is not None:
        entry["confidence"] = round(conf, 4)
        entry["flagged"] = conf < FLAG_BELOW
    if alternatives:
        entry["alternatives"] = [[str(c), round(float(p), 4)] for c, p in alternatives if c]
    out[field] = entry
    return out


def mark_manual(prov: Dict[str, dict], fields: List[str], by: str, note: str = "",
                when: Optional[str] = None) -> Dict[str, dict]:
    """Отметить поля как правленные человеком, сохранив под ней машинный ответ для контекста.

    Подробность важна: через месяц вопрос «почему здесь не так, как у всех остальных» решается
    только этой записью (кто, когда, зачем и что предлагала модель).
    """
    out = dict(prov or {})
    stamp = when or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for field in fields:
        prev = dict(get(out, field))
        entry: Dict[str, Any] = {"source": "manual", "by": by or "unknown", "at": stamp}
        if note:
            entry["note"] = note
        if prev.get("confidence") is not None:
            entry["model_confidence"] = prev.get("confidence")
        if prev.get("alternatives"):
            entry["model_alternatives"] = prev.get("alternatives")
        out[field] = entry
    return out


def clear_manual(prov: Dict[str, dict], fields: List[str], by: str) -> Dict[str, dict]:
    """Снять ручную пометку (право есть только у администратора — проверяется в API).

    Возвращаем поле в состояние «правила модель/не размечено»: значение остаётся, но снова может
    быть перезаписано следующим прогоном.
    """
    out = dict(prov or {})
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for field in fields:
        prev = dict(get(out, field))
        if not prev:
            continue
        prev["source"] = "model"
        prev["manual_cleared_by"] = by or "unknown"
        prev["manual_cleared_at"] = stamp
        out[field] = prev
    return out


def disputed_fields(prov: Dict[str, dict]) -> List[str]:
    """Поля, которые надо показать человеку: низкая уверенность модели и нет ручной правки."""
    out = []
    for field, meta in (prov or {}).items():
        if meta.get("source") == "manual":
            continue
        if meta.get("flagged"):
            out.append(field)
    return sorted(out)


def confidence(prov: Dict[str, dict], field: str) -> Optional[float]:
    try:
        conf = get(prov, field).get("confidence")
        return float(conf) if conf is not None else None
    except (TypeError, ValueError):
        return None


def alternatives(prov: Dict[str, dict], field: str, limit: int = 3) -> List[List[Any]]:
    """Топ-варианты модели по полю: то, между чем она колебалась (для человека это и есть доводы)."""
    alts = get(prov, field).get("alternatives") or []
    out = []
    for item in alts:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append([str(item[0]), item[1]])
    return out[:max(1, limit)]


def as_table_entry(document_id: str, field: str, prov: Dict[str, dict]) -> Dict[str, Any]:
    """Строка для интерфейса разбора: что стоит сейчас, насколько модель уверена, что предлагала."""
    meta = get(prov, field)
    return {
        "document_id": document_id,
        "field": field,
        "field_title": field_title(field),
        "value": meta.get("value"),
        "source": meta.get("source") or "unknown",
        "confidence": meta.get("confidence"),
        "alternatives": alternatives(prov, field),
        "by": meta.get("by"),
        "at": meta.get("at"),
        "note": meta.get("note"),
    }
