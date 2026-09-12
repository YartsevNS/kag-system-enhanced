"""Тесты правок из аудита admin_models.py (2026-09-13).

1. Пароли не попадают в командную строку SSH (sshpass -e + SSHPASS в env,
   sudo-пароль по stdin), имя сервиса санитизируется.
2. Баланс DeepSeek читается из balance_infos (раньше — поле balance → «0.0»),
   для провайдеров без баланса возвращается balance_ok=None, а не ноль.
3. /backup собирает категории динамически (проверяется в живом E2E, здесь —
   что хардкод-список остался только фолбэком).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.api.routes.admin_models import (
    _provider_balance,
    _safe_service_name,
    _ssh_argv_and_env,
)

ROUTES_FILE = Path("src/api/routes/admin_models.py")


# ── 1. SSH: пароли вне командной строки ────────────────────────────────
def _cfg(**kw):
    base = dict(host="192.168.50.41", port=22, username="nick",
                password=None, sudo_password=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_ssh_password_not_in_argv():
    pw = "SuperSecret123"
    argv, env, stdin_text = _ssh_argv_and_env(_cfg(password=pw), "echo OK")
    joined = " ".join(argv)
    assert pw not in joined, "пароль не должен быть в аргументах процесса"
    assert argv[0] == "sshpass" and argv[1] == "-e", "ожидается sshpass -e (пароль из env)"
    assert env.get("SSHPASS") == pw
    assert stdin_text is None


def test_ssh_without_password_uses_plain_ssh():
    argv, env, stdin_text = _ssh_argv_and_env(_cfg(), "echo OK")
    assert argv[0] == "ssh"
    assert "SSHPASS" not in env
    assert stdin_text is None


def test_sudo_password_goes_to_stdin():
    sudo_pw = "SudoSecret456"
    argv, env, stdin_text = _ssh_argv_and_env(_cfg(sudo_password=sudo_pw), "sudo -S -p '' echo OK")
    assert sudo_pw not in " ".join(argv)
    assert stdin_text == sudo_pw + "\n", "sudo-пароль должен уходить по stdin"


def test_service_name_is_sanitized():
    assert _safe_service_name("ollama") == "ollama"
    assert _safe_service_name("ollama.service") == "ollama.service"
    for evil in ("ollama; rm -rf /", "ollama && curl x", "`id`", "a\nb", ""):
        assert _safe_service_name(evil) == "ollama", f"инъекция не отсечена: {evil!r}"


def test_no_shell_true_and_no_sshpass_p_in_source():
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert "shell=True" not in src, "не должно быть вызовов через shell"
    ssh_src = Path("src/api/services/ssh_manager.py").read_text(encoding="utf-8")
    # именно построение argv с паролем-аргументом (-p), а не упоминание в тексте
    for text, name in ((src, "admin_models.py"), (ssh_src, "ssh_manager.py")):
        assert '"sshpass", "-p"' not in text, f"{name}: пароль нельзя давать sshpass аргументом"
    assert '"sshpass", "-e"' in ssh_src, "ssh_manager должен брать пароль из env (sshpass -e)"


# ── 2. Баланс провайдеров ──────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Подменяет httpx.AsyncClient: отдаёт заранее заданный ответ."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, **kw):
        return self._response


def _run_balance(monkeypatch, payload, status=200, provider="deepseek",
                 url="https://api.deepseek.com", key="k"):
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(_FakeResponse(status, payload)))
    return asyncio.run(_provider_balance(provider, url, key))


def test_deepseek_balance_read_from_balance_infos(monkeypatch):
    payload = {"is_available": True,
               "balance_infos": [{"currency": "CNY", "total_balance": "42.50",
                                  "granted_balance": "2.50", "topped_up_balance": "40.00"}]}
    res = _run_balance(monkeypatch, payload)
    assert res["balance"] == 42.5, "должно читаться total_balance из balance_infos"
    assert res["balance_ok"] is True
    assert res["balance_known"] is True
    assert "42.50 CNY" in res["display"]


def test_deepseek_unavailable_account_marks_not_ok(monkeypatch):
    payload = {"is_available": False, "balance_infos": [{"currency": "USD", "total_balance": "0.00"}]}
    res = _run_balance(monkeypatch, payload)
    assert res["balance_ok"] is False
    assert "недоступен" in res["message"]


def test_negative_balance_is_not_ok(monkeypatch):
    payload = {"is_available": True,
               "balance_infos": [{"currency": "CNY", "total_balance": "-1.18"}]}
    res = _run_balance(monkeypatch, payload)
    assert res["balance_ok"] is False, "исчерпанный/отрицательный баланс не может быть «ок»"
    assert "исчерпан" in res["message"]
    assert res["balance"] == -1.18


def test_gigachat_reports_unknown_not_zero(monkeypatch):
    res = _run_balance(monkeypatch, {"whatever": 1}, provider="gigachat",
                       url="https://gigachat.devices.sberbank.ru/api/v1")
    assert res["balance_ok"] is None, "неизвестный баланс — это не «0»"
    assert res["balance_known"] is False
    assert "не реализована" in res["message"]


def test_ollama_is_unlimited_without_http(monkeypatch):
    res = _run_balance(monkeypatch, {}, provider="ollama", url="", key="")
    assert res["balance_ok"] is True and res["display"] == "∞"


def test_no_api_key_reported(monkeypatch):
    res = _run_balance(monkeypatch, {}, key="")
    assert res["balance_ok"] is False and "не указан" in res["message"]


def test_http_error_is_not_zero_balance(monkeypatch):
    res = _run_balance(monkeypatch, {}, status=401)
    assert res["balance_ok"] is False
    assert "недействителен" in res["message"]
    assert res.get("balance") is None, "при ошибке нельзя показывать баланс"


# ── 3. Бэкап: хардкод только как фолбэк ────────────────────────────────
def test_backup_categories_are_dynamic_with_fallback():
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert "def _all_config_categories" in src, "должен быть динамический сбор категорий"
    assert "SELECT DISTINCT category FROM system_configs" in src
    assert "include_caches" in src, "кэши (entity_cache) пропускаются по умолчанию"
    assert "BACKUP_CACHE_CATEGORIES" in src
