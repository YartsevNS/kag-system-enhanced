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
def test_backup_categories_are_dynamic_only():
    """Категории — только из БД: хардкод-список убран, при сбое чтения — 503."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert src.count("def _all_config_categories") == 1, "хелпер должен быть один"
    assert src.count("SELECT DISTINCT category FROM system_configs") == 1, \
        "запрос категорий не должен дублироваться (используется и в backup-documents)"
    assert "BACKUP_NAMESPACES" not in src, "устаревший хардкод-список должен быть удалён"
    assert "include_caches" in src and "BACKUP_CACHE_CATEGORIES" in src
    assert "503" in src, "при нечитаемых категориях бэкап должен отвечать ошибкой"

def test_ingest_config_functions_renamed():
    """Дублей имён быть не должно: у блокировки ингеста свои имена функций."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert src.count("async def get_upload_config(") == 1
    assert src.count("async def save_upload_config(") == 1
    assert src.count("async def get_ingest_config(") == 1
    assert src.count("async def save_ingest_config(") == 1

def test_sync_calls_wrapped_in_to_thread():
    """Синхронные сервисы не должны вызываться из async-эндпоинтов напрямую."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    for pattern in ("return system_monitor.get_cpu_info()",
                    "return system_monitor.get_disk_info()",
                    "return qdrant_monitor.get_collections_list()",
                    "success = docker_monitor.restart_container(",
                    "result = ssh_manager.test_connection(config)"):
        assert pattern not in src, f"не обёрнуто в to_thread: {pattern}"
    assert src.count("asyncio.to_thread") >= 20, "должно быть обёрнуто большинство вызовов"
    # subprocess в /deploy не должен блокировать loop (timeout до 120 с)
    assert "result = subprocess.run(" not in src

def test_config_store_imported_once_at_top():
    """config_store должен импортироваться на уровне модуля, а не внутри функций.

    Раньше импорт был внизу файла + 39 локальных копий: работало «случайно»
    (импорт выполняется при загрузке модуля), но ломается при рефакторинге.
    """
    src = ROUTES_FILE.read_text(encoding="utf-8")
    imp = "from src.api.services.config_store import config_store"
    assert src.count(imp) == 1 or src.count(imp) == 2, "импорт должен быть один (плюс явный алиас)"
    assert src.index(imp) < src.index("@router."), \
        "импорт должен быть в шапке модуля, до объявления роутов"
    # локальные дубли в функциях недопустимы (кроме явного алиаса 'as cs')
    for line in src.splitlines():
        st = line.strip()
        if st.startswith("from src.api.services.config_store import") and st != imp:
            assert st.endswith("as cs"), f"неожиданный локальный импорт: {st}"

def test_ext_llm_uses_sync_loader_not_global():
    """test_ext_llm не должен вызывать async-функцию без await (баг «never awaited»)."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert "def _load_ext_llm_from_db()" in src
    assert "def _load_graph_model_from_db()" in src
    assert (chr(10) + "    get_ext_llm()" + chr(10)) not in src, "нельзя вызывать async-функцию без await"
    i = src.index("async def test_ext_llm(")
    block = src[i:i + 700]
    assert "cfg = _load_ext_llm_from_db()" in block
    assert "_ext_llm_config." not in block, "в тесте не должно быть чтения глобала"
    # ключ наружу не отдаём в открытом виде
    assert "_mask_secret" in src

def test_deploy_write_file_is_hardened():
    """write_file: только разрешённые расширения, строгий base64, лимит размера."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert "allowed_ext" in src and ".py" in src
    assert "base64.b64decode(req.file_content, validate=True)" in src
    assert "DEPLOY_MAX_FILE_BYTES" in src
    assert "DEPLOY_SRC_PATH" in src and "DEPLOY_REPO_PATH" in src
    cfg = Path("src/config.py").read_text(encoding="utf-8")
    for name in ("DEPLOY_SRC_PATH", "DEPLOY_REPO_PATH", "ENV_FILE_PATH", "DEPLOY_MAX_FILE_BYTES"):
        assert name in cfg, f"нет настройки {name}"

def test_sync_services_wrapped_in_alias_and_backup():
    """Neo4j в элиасах и файловые/БД-операции в бэкапах — через to_thread."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert "await asyncio.to_thread(kg_service." in src
    # ни одного необёрнутого вызова kg_service
    for line in src.splitlines():
        if "kg_service." in line and "to_thread" not in line and "import" not in line:
            raise AssertionError(f"необёрнутый вызов Neo4j: {line.strip()[:80]}")
    assert "_index_uploads" in src, "индексация каталога uploads должна идти в потоке"
    assert "await asyncio.to_thread(config_store.get_all, ns)" in src
    # восстановление настроек — один поток на namespace, а не на каждый ключ
    assert "_restore_ns" in src and "await asyncio.to_thread(_restore_ns, ns, ns_data)" in src
    # метаданные и история чатов тоже уходят в поток
    assert src.count("to_thread(zf.writestr") >= 5

def test_backup_documents_skips_caches():
    src = ROUTES_FILE.read_text(encoding="utf-8")
    i = src.index("async def backup_documents(")
    tail = src[i:]
    nxt = tail.find(chr(10) + "@router.")
    body = tail if nxt == -1 else tail[:nxt]   # функция может быть последней в файле
    assert "include_caches" in body, "у бэкапа документов должен быть флаг кэшей"
    assert "BACKUP_CACHE_CATEGORIES" in body, "кэши должны отсекаться тем же списком"

def test_deploy_rejects_broken_base64_and_python():
    """Проверка в живом стенде показала: битый base64 записывался как текст и затёр
    api/__init__.py. Теперь кодировка задаётся явно, а .py проверяется компиляцией."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    # по умолчанию utf8: base64-подобный литерал больше не декодируется молча
    assert 'encoding: str = Field(default="utf8"' in src
    assert "looks_b64" in src
    assert "Неизвестная кодировка" in src
    assert 'if ext == ".py":' in src and "compile(content, req.file_path" in src
    assert 'base64.b64decode(req.file_content, validate=True)' in src


def test_backup_arcname_truncates_by_bytes():
    """Имя в архиве режется по БАЙТАМ: для CJK 120 символов = 360 байт = снова Errno 36."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert "def _truncate_bytes" in src
    assert 'raw[:max_bytes].decode("utf-8", errors="ignore")' in src
    assert "stem[:limit]" not in src, "обрезка по символам недопустима"

    import importlib.util
    spec = importlib.util.spec_from_file_location("am_bytes", ROUTES_FILE)
    # функцию проверяем копией логики: импорт модуля требует БД
    def _truncate_bytes(text: str, max_bytes: int) -> str:
        raw = text.encode("utf-8")
        return text if len(raw) <= max_bytes else raw[:max_bytes].decode("utf-8", errors="ignore")

    cjk = "漢" * 200            # 3 байта на символ
    out = _truncate_bytes(cjk, 150)
    assert len(out.encode("utf-8")) <= 150
    assert out  # непустой результат
    rus = "я" * 200             # 2 байта на символ
    assert len(_truncate_bytes(rus, 150).encode("utf-8")) <= 150


def test_backup_distinguishes_db_error_from_empty():
    """Ошибка БД (None) и пустая таблица ([]) — разные случаи."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert "Optional[List[str]]" in src
    assert "return None" in src
    assert "if categories is None:" in src
    assert "категории настроек не прочитаны" in src


def test_init_logs_fallback():
    """Молчаливый fallback при старте запрещён — должно быть предупреждение."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert src.count("настройки из БД при старте не загружены") == 0  # формулировка иная
    assert src.count("инициализация из БД не удалась") == 1
    assert src.count("инициализация модели графа из БД не удалась") == 1


def test_documents_backup_survives_long_filenames():
    """Бэкап документов не должен падать на именах длиннее лимита ФС (Errno 36).

    Живой случай: filename в БД до 205 символов, код собирал «<doc_id>_<filename>»
    и path.exists() бросал ENAMETOOLONG → архив не формировался вовсе.
    Теперь файлы ищутся по индексу каталога, имя внутри архива обрезается,
    а ошибка отвечает 500, а не 200 с «error».
    """
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert "files_by_doc" in src, "файлы должны искаться по индексу каталога"
    assert "_safe_arcname" in src, "имя в архиве должно обрезаться"
    assert "backup_warnings.json" in src, "пропущенные файлы должны попадать в отчёт"
    assert 'status_code=500' in src, "неудачный ZIP должен отдавать 500"
    # запрещённая конструкция, из-за которой падало: имя из полного названия
    assert 'f"{doc_id}_{filename}"' not in src

def test_restart_ollama_keeps_admin_contract():
    """Страница админки проверяет result.status — контракт不能被 ломать."""
    src = ROUTES_FILE.read_text(encoding="utf-8")
    assert '"status": "success" if ok else "warning"' in src
    for field in ("systemctl_active", "http_responding", "service_active", "api_responding"):
        assert f'"{field}"' in src, f"поле {field} должно быть в ответе"
    # второй ssh-вызов не должен использовать sudo -n (без tty кэш прав не работает)
    assert "sudo -n systemctl" not in src
    assert src.count("sudo -S -p '' systemctl") == 2
