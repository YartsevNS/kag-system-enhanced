"""Доступ к документу: ACL, владелец, админ (юниты на document_access).

Симптом, который закрывают тесты: ACL применялся только в фильтре списка,
а отдельные эндпоинты документа (/details, /chunks, /preview, /thumbnail и др.)
отдавали содержимое любому авторизованному пользователю — приватный документ
читался по прямой ссылке.
"""
import pytest
from fastapi import HTTPException

from src.api.services.document_access import (
    can_read,
    ensure_can_read,
    ensure_owner_or_admin,
    is_owner_or_admin,
    parse_id_list,
)


class _User:
    def __init__(self, uid, is_admin=False, groups=()):
        self.id = uid
        self.is_admin = is_admin
        self.groups = [type("G", (), {"id": g})() for g in groups]


def _meta(**kw):
    base = {"document_id": "d1", "uploaded_by": "u-owner", "visibility": "public"}
    base.update(kw)
    return base


def test_parse_id_list_formats():
    assert parse_id_list('["a","b"]') == ["a", "b"]
    assert parse_id_list("{a,b}") == ["a", "b"]
    assert parse_id_list("a,b") == ["a", "b"]
    assert parse_id_list(["a", "", None]) == ["a"]
    assert parse_id_list(None) == []
    assert parse_id_list("") == []


def test_public_document_visible_to_everyone():
    assert can_read(_meta(), _User("u-1"))
    assert can_read(_meta(), None)  # аноним видит только public


def test_admin_sees_everything():
    assert can_read(_meta(visibility="restricted"), _User("u-9", is_admin=True))


def test_owner_sees_own_restricted_document():
    assert can_read(_meta(visibility="restricted"), _User("u-owner"))


def test_restricted_hidden_from_others():
    assert not can_read(_meta(visibility="restricted"), _User("u-2"))
    assert not can_read(_meta(visibility="restricted"), None)


def test_restricted_visible_by_allow_list():
    meta = _meta(visibility="restricted", allow_user_ids='["u-2"]')
    assert can_read(meta, _User("u-2"))
    assert not can_read(meta, _User("u-3"))


def test_restricted_visible_by_group():
    meta = _meta(visibility="restricted", allow_group_ids='["g-1"]')
    assert can_read(meta, _User("u-2", groups=["g-1"]))
    assert not can_read(meta, _User("u-2", groups=["g-2"]))


def test_deny_has_priority():
    meta = _meta(deny_user_ids='["u-2"]')
    assert not can_read(meta, _User("u-2"))
    meta_g = _meta(deny_group_ids='["g-1"]')
    assert not can_read(meta_g, _User("u-2", groups=["g-1"]))
    # админ не ограничен deny
    assert can_read(meta, _User("u-2", is_admin=True))


def test_deny_beats_owner():
    """Владелец видит свой документ, но явный deny сильнее."""
    meta = _meta(uploaded_by="u-2", deny_user_ids='["u-2"]')
    assert can_read(meta, _User("u-2", is_admin=True))       # админ — да
    assert can_read(_meta(uploaded_by="u-2"), _User("u-2"))  # без deny — да


def test_legacy_group_ids_restrict_access():
    meta = _meta(group_ids='["g-1"]')
    assert can_read(meta, _User("u-2", groups=["g-1"]))
    assert not can_read(meta, _User("u-3", groups=["g-9"]))


def test_owner_or_admin():
    assert is_owner_or_admin(_meta(), _User("u-owner"))
    assert is_owner_or_admin(_meta(), _User("u-9", is_admin=True))
    assert not is_owner_or_admin(_meta(), _User("u-2"))
    assert not is_owner_or_admin(_meta(uploaded_by=None), _User("u-2"))


def test_ensure_can_read_raises(monkeypatch):
    with pytest.raises(HTTPException) as e404:
        ensure_can_read("нет-такого", _User("u-1"), meta={})
    assert e404.value.status_code == 404

    with pytest.raises(HTTPException) as e403:
        ensure_can_read("d1", _User("u-2"), meta=_meta(visibility="restricted"))
    assert e403.value.status_code == 403


def test_ensure_owner_or_admin_raises():
    with pytest.raises(HTTPException) as e:
        ensure_owner_or_admin("d1", _User("u-2"), meta=_meta())
    assert e.value.status_code == 403
