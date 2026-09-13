"""Единая проверка доступа к документу (ACL + владелец + админ).

Зачем модуль: ACL (visibility + allow/deny) применялся ТОЛЬКО в фильтре списка
документов. Отдельные эндпоинты (`/details`, `/chunks`, `/preview`,
`/thumbnail`, `/status`, `/versions`, `/diff`, `/ocr`, `/tables`, `/access`)
отдавали содержимое и метаданные любому авторизованному пользователю — то есть
приватный документ можно было прочитать по прямой ссылке, минуя список.

Логика ровно та же, что в фильтре `/list` (deny имеет приоритет; `restricted`
доступен только по allow), плюс две добавки, без которых смысл ACL теряется:

* владелец видит СВОЙ документ (в списке этого не было: свой же restricted
  документ мог пропасть из выдачи);
* админ видит всё.

Модуль намеренно без зависимостей от роутеров: его импортируют и роуты, и тесты.
"""
from __future__ import annotations

import json
from typing import Any, Optional


def parse_id_list(raw: Any) -> list:
    """Разобрать список id из JSON-строки, PostgreSQL-массива, списка или CSV."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(x) for x in raw if x not in (None, "")]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
            if isinstance(value, list):
                return [str(x) for x in value if x not in (None, "")]
        except Exception:
            pass
        if text.startswith("{") and text.endswith("}"):
            inner = text[1:-1].strip()
            if not inner:
                return []
            return [x.strip().strip('"') for x in inner.split(",") if x.strip()]
        return [x.strip() for x in text.split(",") if x.strip()]
    return []


def _user_group_ids(user) -> set:
    try:
        groups = getattr(user, "groups", None) or []
        return {str(getattr(g, "id", g)) for g in groups}
    except Exception:
        return set()


def can_read(meta: dict, user) -> bool:
    """Может ли пользователь читать документ (та же семантика, что в /list)."""
    if not isinstance(meta, dict) or not meta:
        return False
    if user is None:
        return (meta.get("visibility") or "public") == "public"
    if bool(getattr(user, "is_admin", False)):
        return True

    uid = str(getattr(user, "id", "") or "")
    if uid and str(meta.get("uploaded_by") or "") == uid:
        return True  # владелец — всегда свой документ

    groups = _user_group_ids(user)
    if uid and uid in set(parse_id_list(meta.get("deny_user_ids"))):
        return False
    if groups and (groups & set(parse_id_list(meta.get("deny_group_ids")))):
        return False

    # legacy: payload-группы документа (group_ids)
    doc_groups = set(parse_id_list(meta.get("group_ids")))
    if doc_groups and not (groups & doc_groups):
        return False

    if (meta.get("visibility") or "public") == "public":
        return True
    if uid and uid in set(parse_id_list(meta.get("allow_user_ids"))):
        return True
    return bool(groups & set(parse_id_list(meta.get("allow_group_ids"))))


def is_owner_or_admin(meta: dict, user) -> bool:
    """Владелец документа или админ (для операций над документом)."""
    if not isinstance(meta, dict) or not meta or user is None:
        return False
    if bool(getattr(user, "is_admin", False)):
        return True
    uid = str(getattr(user, "id", "") or "")
    owner = str(meta.get("uploaded_by") or "")
    return bool(uid and owner and uid == owner)


def load_meta(document_id: str) -> dict:
    """Метаданные документа из БД (пустой dict, если документа нет)."""
    from src.api.services.document_repository import get_doc_repo
    return get_doc_repo().get_dict(document_id) or {}


def ensure_can_read(document_id: str, user, meta: Optional[dict] = None) -> dict:
    """Проверить доступ к документу: 404 если нет, 403 если нельзя читать."""
    from fastapi import HTTPException

    if meta is None or not isinstance(meta, dict):
        meta = load_meta(document_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if not can_read(meta, user):
        raise HTTPException(status_code=403, detail="Недостаточно прав: документ недоступен")
    return meta


def ensure_owner_or_admin(document_id: str, user, meta: Optional[dict] = None) -> dict:
    """Проверить, что пользователь владелец документа или админ."""
    from fastapi import HTTPException

    if meta is None or not isinstance(meta, dict):
        meta = load_meta(document_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if not is_owner_or_admin(meta, user):
        raise HTTPException(
            status_code=403,
            detail="Недостаточно прав: операция доступна владельцу документа или администратору",
        )
    return meta
