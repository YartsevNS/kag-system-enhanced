"""Страница разбора спорной разметки: список спорных значений и ручная правка человеком.

Зачем отдельный роут, а не админский `/admin/models/document-classification`. Разметку ставят модель
и правила, и на спорных документах она колеблется. Разбирать эти случаи должен тот, КТО ЗАГРУЗИЛ
документ (он знает предмет), а не только администратор. Поэтому:

  * список спорных значений виден любому вошедшему — но только по СВОИМ документам (админ видит все);
  * правка человеком ставит в метаданных пометку `manual` (кто, когда, зачем) — см.
    `src/indexing/label_provenance.py`;
  * значение с пометкой `manual` заново НЕ правит ни модель, ни прибор; изменить его может только
    администратор (или снять пометку и вернуть поле машине — тоже только администратор).

Админский путь `/admin/models/document-classification` остаётся как есть: он для служебных правок
и работает по любому документу без ограничений.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.middleware.auth_v2 import get_current_admin, get_current_user
from src.database.user_models import User
from src.indexing import label_provenance as prov_mod

logger = logging.getLogger(__name__)

router = APIRouter()


class Decision(BaseModel):
    """Решение человека по спорному значению."""

    document_id: str
    field: str                                   # rubrics | facets.<код> | document_type
    value: Union[str, List[str]]
    note: Optional[str] = None                   # зачем так решили (попадает в метаданные)


class Unlock(BaseModel):
    """Снять ручную пометку (только администратор): поле снова сможет править модель."""

    document_id: str
    field: str


def _is_admin(user: User) -> bool:
    return bool(getattr(user, "is_admin", False))


def _parse_json(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default
    return default


def _validate_value(field: str, value: Union[str, List[str]]) -> tuple[bool, str, Any]:
    """Проверить значение по ЗАКРЫТОМУ словарю и вернуть его в канонической форме.

    Молчаливое «не знаю такого значения, оставлю как есть» здесь недопустимо: опечатка человека
    должна быть видна ему сразу, а не превращаться в значение, которого не понимают ни фильтры,
    ни подписи (та же логика, что в админском эндпоинте правки разметки).
    """
    from src.indexing import document_facets, document_kinds, document_topics

    raw_items = value if isinstance(value, (list, tuple)) else [value]
    items = [str(v or "").strip().lower() for v in raw_items if str(v or "").strip()]

    if field == "rubrics":
        bad = [v for v in items if not document_topics.is_valid(v)]
        if bad:
            return False, (f"тема(ы) {bad} отсутствуют в словаре {document_topics.TOPICS_VERSION}; "
                           "допустимые: " + document_topics.vocabulary_line()), None
        return True, "", document_topics.normalize(items)

    if field == "document_type":
        if len(items) != 1 or not document_kinds.is_valid(items[0]):
            return False, (f"вид «{', '.join(items) or '—'}» отсутствует в словаре "
                           f"{document_kinds.VOCABULARY_VERSION}; допустимые: "
                           + document_kinds.vocabulary_line()), None
        return True, "", items[0]

    if field.startswith("facets."):
        facet = field.split(".", 1)[1]
        if not document_facets.is_valid_facet(facet):
            return False, f"фасета «{facet}» нет в словаре фасетов; допустимые: " + \
                   ", ".join(document_facets.FACET_CODES), None
        bad = [v for v in items if not document_facets.is_valid_value(facet, v)]
        if bad:
            return False, (f"значения {bad} вне перечня фасета «{facet}»; допустимые: "
                           + document_facets.vocabulary_line()), None
        return True, "", items

    return False, f"поле «{field}» не поддерживается ручной правкой", None


def _apply_change(doc: dict, field: str, value: Any) -> tuple[dict, dict]:
    """Что записать в документ: значение поля в канонической форме + происхождение."""
    prov = prov_mod.parse(doc.get("label_provenance"))
    changes: dict = {}

    if field == "rubrics":
        changes["rubrics"] = json.dumps(value, ensure_ascii=False)
        stored: Any = value
    elif field == "document_type":
        changes["document_type"] = value
        stored = value
    else:
        facet = field.split(".", 1)[1]
        facets = _parse_json(doc.get("facets"), {})
        if not isinstance(facets, dict):
            facets = {}
        facets[facet] = value if isinstance(value, list) else [value]
        changes["facets"] = json.dumps(facets, ensure_ascii=False)
        stored = facets[facet]

    prov = prov_mod.mark_model(prov, field, stored, prov_mod.confidence(prov, field),
                               prov_mod.alternatives(prov, field))
    return changes, prov


async def _write(doc_id: str, changes: dict, prov: dict, actor: str, note: str,
                 fields: List[str]) -> None:
    """Записать значения, происхождение и обновить payload Qdrant (иначе фильтры не увидят правку)."""
    from src.api.services.document_repository import get_doc_repo

    prov = prov_mod.mark_manual(prov, fields, by=actor, note=note or "")
    changes["label_provenance"] = prov_mod.dump(prov)
    get_doc_repo().upsert(doc_id, changes)
    try:
        from src.indexing.embeddings_service import service_for_document

        await service_for_document(doc_id).update_document_payload(doc_id, {
            k: (_parse_json(v, v) if k in ("topics", "facets", "rubrics") else v)
            for k, v in changes.items()
        })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[labels] payload не обновлён для {doc_id[:12]}: {e}")


def _journal(actor: str, action: str, target: str, details: dict) -> None:
    try:
        from src.security.provenance import append_action

        append_action(actor=actor, action=action, target=target, details=details)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[labels] журнал действий недоступен: {e}")


@router.get("/review", summary="Спорные значения разметки (для разбора человеком)")
async def review_list(limit: int = 200, only: str = "", current_user: User = Depends(get_current_user)):
    """Список значений, которые надо посмотреть человеку.

    Что попадает: значения модели с уверенностью ниже порога (спорные) и значения, которые человек
    правил руками (`manual` — их показываем, чтобы правку было видно и можно было пересмотреть).
    Область: свои документы; администратор видит все. Параметр `only`: `disputed` | `manual` | пусто.
    """
    from src.api.services.document_repository import get_doc_repo

    admin = _is_admin(current_user)
    items: List[dict] = []
    docs_total = 0
    manual_total = 0
    disputed_total = 0

    for doc_id, doc in (get_doc_repo().get_all() or {}).items():
        if not admin and str(doc.get("uploaded_by") or "") != str(getattr(current_user, "id", "")):
            continue
        prov = prov_mod.parse(doc.get("label_provenance"))
        manual = prov_mod.manual_fields(prov)
        disputed = prov_mod.disputed_fields(prov)
        if not prov:
            # Разметки нет вовсе (документ ещё не размечен) — это тоже работа для человека, но
            # отдельным видом: «нет разметки» нельзя показывать как «спорное значение модели».
            disputed = ["rubrics"] if not _parse_json(doc.get("rubrics"), []) else []
        if not manual and not disputed:
            continue

        docs_total += 1
        for field in manual:
            if only and only != "manual":
                continue
            manual_total += 1
            entry = prov_mod.as_table_entry(doc_id, field, prov)
            entry.update({
                "kind": "manual",
                "filename": doc.get("filename"),
                "title": doc.get("recognized_title") or doc.get("filename"),
                "value": _current_value(doc, field, prov),
                "editable": admin,
                "blocked_reason": None if admin else
                (f"значение правил {entry.get('by') or 'человек'} "
                 f"{(entry.get('at') or '')[:10]}; изменить может только администратор"),
            })
            items.append(entry)
        for field in disputed:
            if only and only != "disputed":
                continue
            disputed_total += 1
            entry = prov_mod.as_table_entry(doc_id, field, prov)
            entry.update({
                "kind": "disputed",
                "filename": doc.get("filename"),
                "title": doc.get("recognized_title") or doc.get("filename"),
                "value": _current_value(doc, field, prov),
                "editable": True,
                "blocked_reason": None,
            })
            items.append(entry)

    items.sort(key=lambda i: (i.get("confidence") if i.get("confidence") is not None else 2.0))
    return {
        "items": items[:max(1, min(1000, int(limit or 200)))],
        "counts": {"documents": docs_total, "disputed": disputed_total, "manual": manual_total,
                   "shown": len(items)},
        "can_admin": admin,
        "flag_below": prov_mod.FLAG_BELOW,
    }


def _current_value(doc: dict, field: str, prov: dict) -> Any:
    """Что стоит в документе СЕЙЧАС (а не что предлагала модель): показывать надо факт."""
    if field == "rubrics":
        return _parse_json(doc.get("rubrics"), [])
    if field == "document_type":
        return doc.get("document_type") or ""
    facets = _parse_json(doc.get("facets"), {})
    facet = field.split(".", 1)[1]
    return (facets or {}).get(facet) or []


@router.post("/review/decide", summary="Решить спорное значение руками")
async def decide(payload: Decision, current_user: User = Depends(get_current_user)):
    """Человек ставит значение сам. Значение проверяется словарём, в метаданных появляется пометка
    `manual` (кто, когда, зачем), и такое значение дальше не перезаписывается моделью.
    """
    from src.api.services.document_repository import get_doc_repo

    repo = get_doc_repo()
    doc = repo.get_dict(payload.document_id) or {}
    if not doc:
        raise HTTPException(status_code=404, detail="документ не найден")

    admin = _is_admin(current_user)
    if not admin and str(doc.get("uploaded_by") or "") != str(getattr(current_user, "id", "")):
        raise HTTPException(status_code=403,
                            detail="Можно править только свои документы — обращайтесь к администратору")

    prov = prov_mod.parse(doc.get("label_provenance"))
    if prov_mod.is_manual(prov, payload.field) and not admin:
        who = prov_mod.get(prov, payload.field).get("by") or "человек"
        raise HTTPException(
            status_code=403,
            detail=(f"Значение уже правил {who}. Повторная правка ручного значения — только "
                    "администратор."))

    ok, message, value = _validate_value(payload.field, payload.value)
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    changes, prov = _apply_change(doc, payload.field, value)
    actor = getattr(current_user, "username", None) or getattr(current_user, "email", "") or "user"
    await _write(payload.document_id, changes, prov, actor=actor, note=payload.note or "",
                 fields=[payload.field])
    _journal(actor, "label_review_decide", payload.document_id, {
        "field": payload.field, "value": value, "note": payload.note or "",
        "before": _current_value(doc, payload.field, prov),
    })
    return {"status": "ok", "document_id": payload.document_id, "field": payload.field,
            "value": value, "manual": True}


@router.post("/review/unlock", summary="Снять ручную пометку (только администратор)")
async def unlock(payload: Unlock, current_user: User = Depends(get_current_admin)):
    """Вернуть поле машине: правка человека снимается, и следующий прогон модели снова сможет
    перезаписать значение. Отдельное действие, а не побочный эффект правки, — иначе ручной след
    исчезает незаметно вместе с чужим решением.
    """
    from src.api.services.document_repository import get_doc_repo

    repo = get_doc_repo()
    doc = repo.get_dict(payload.document_id) or {}
    if not doc:
        raise HTTPException(status_code=404, detail="документ не найден")

    prov = prov_mod.parse(doc.get("label_provenance"))
    if not prov_mod.is_manual(prov, payload.field):
        return {"status": "noop", "message": "поле не помечено как ручное"}

    actor = getattr(current_user, "username", None) or "admin"
    prov = prov_mod.clear_manual(prov, [payload.field], by=actor)
    repo.upsert(payload.document_id, {"label_provenance": prov_mod.dump(prov)})
    _journal(actor, "label_review_unlock", payload.document_id, {"field": payload.field})
    return {"status": "ok", "document_id": payload.document_id, "field": payload.field,
            "manual": False}
