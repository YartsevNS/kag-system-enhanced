"""Структурные проверки upload.py: права на местах, кэш без объекта пользователя.

Дополняют юниты на document_access: юниты проверяют логику, эти — что она
действительно подключена к эндпоинтам (иначе логика есть, а доступа нет).
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "src/api/routes/upload.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)

READ_ENDPOINTS = [
    "get_document_details", "get_document_chunks", "get_document_preview",
    "get_document_thumbnail", "get_document_status", "get_document_versions",
    "diff_document_versions", "check_ocr", "view_ocr_markdown",
    "get_document_tables", "search_document_tables", "get_document_access",
]
ADMIN_ONLY = ["reanalyze_all_documents", "reindex_all_documents",
              "reprocess_pending_documents", "reprocess_ocr"]


def _func(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"функция не найдена: {name}")


def _signature(name):
    return ast.unparse(_func(name).args)


def _body(name):
    node = _func(name)
    return ast.unparse(node)


def test_read_endpoints_require_auth_and_acl():
    for name in READ_ENDPOINTS:
        sig = _signature(name)
        assert "Depends" in sig, f"{name}: нет Depends — эндпоинт без проверки прав"
        assert "ensure_can_read" in _body(name), (
            f"{name}: нет проверки ACL — документ отдаётся без учёта visibility/allow/deny"
        )


def test_mass_operations_are_admin_only():
    for name in ADMIN_ONLY:
        assert "get_current_admin" in _signature(name), f"{name}: массовая операция не admin-only"


def test_reindex_and_process_check_owner():
    assert "ensure_owner_or_admin" in _body("reindex_document"), "reindex без проверки владельца"
    body = _body("process_document_now")
    assert "Требуется аутентификация" in body, "process пускает анонима/системный документ без админа"
    assert "системный документ" in body, "process не ограничивает системные документы админом"


def test_tus_handlers_check_session_owner():
    for name in ("tus_head", "tus_patch", "tus_delete"):
        body = _body(name)
        assert "_tus_check_owner" in body, f"{name}: нет проверки владельца TUS-сессии"
        assert "Depends" in _signature(name), f"{name}: нет зависимости авторизации"


def test_tus_create_respects_upload_block():
    assert "_deny_if_uploads_blocked()" in _body("tus_create"), (
        "TUS создаёт сессию в обход блокировки загрузки"
    )


def test_cache_key_has_no_model_objects():
    assert "_cache_key" in SRC, "нет нормализации ключа кэша"
    key_fn = _body("_cache_key")
    assert "id" in key_fn and "username" in key_fn, (
        "ключ кэша должен брать у объектов стабильный id, а не адрес памяти"
    )
    assert "_CACHE_MAX" in SRC, "кэш не ограничен по размеру"
