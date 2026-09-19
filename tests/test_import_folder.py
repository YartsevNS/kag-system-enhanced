"""Импорт файлов из папки (пакетно): безопасность пути, отбор файлов, права.

Зачем: админ кладёт файлы на диск стенда (scp/флешка) и забирает их пачкой из админки,
не таща через браузер. Важно: наружу из каталога импорта выйти нельзя, и забрать можно
только разрешённые расширения.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "src/api/routes/upload.py").read_text(encoding="utf-8")


def _route_module():
    from src.api.routes import upload as mod

    return mod


@pytest.fixture
def import_dir(monkeypatch, tmp_path):
    base = tmp_path / "inbox"
    base.mkdir()
    mod = _route_module()
    monkeypatch.setattr(mod._settings, "IMPORT_BASE_DIR", str(base))
    return base


def test_resolve_inside_base(import_dir):
    mod = _route_module()
    sub = mod._resolve_import_dir("batch-1")
    assert str(sub).startswith(str(import_dir))
    assert mod._resolve_import_dir("") == import_dir


@pytest.mark.parametrize("bad", ["../..", "../../etc", "/etc", "sub/../../.."])
def test_resolve_rejects_escape(import_dir, bad):
    from fastapi import HTTPException

    mod = _route_module()
    with pytest.raises(HTTPException) as exc:
        mod._resolve_import_dir(bad)
    assert exc.value.status_code == 400


def test_scan_filters_extensions_and_service_files(import_dir):
    mod = _route_module()
    (import_dir / "a.pdf").write_text("x")
    (import_dir / "b.PDF").write_text("x")          # регистр расширения не важен
    (import_dir / "c.exe").write_text("x")          # не разрешено
    (import_dir / ".hidden.pdf").write_text("x")    # служебное — пропуск
    (import_dir / "__meta.pdf").write_text("x")     # служебное — пропуск
    sub = import_dir / "sub"
    sub.mkdir()
    (sub / "d.md").write_text("x")

    names = sorted(p.name for p in mod._scan_import_dir("", recursive=True))
    assert names == ["a.pdf", "b.PDF", "d.md"], names

    flat = sorted(p.name for p in mod._scan_import_dir("", recursive=False))
    assert flat == ["a.pdf", "b.PDF"], flat


def test_scan_of_missing_folder_is_empty(import_dir):
    mod = _route_module()
    assert mod._scan_import_dir("нет-такой-папки") == []


def _body(name: str) -> str:
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"функция не найдена: {name}")


def test_endpoints_require_admin():
    for name in ("import_folder_scan", "import_folder"):
        assert "_require_admin" in _body(name), f"{name} должна требовать администратора"


def test_import_respects_uploads_blocked_flag():
    """Импорт — та же загрузка: при выключенной загрузке он тоже должен отказывать."""
    assert "_deny_if_uploads_blocked" in _body("import_folder")


def test_import_deduplicates_before_saving():
    body = _body("import_folder")
    assert "find_by_hash" in body, "дубликаты нужно отсекать до записи, иначе не отличить их в отчёте"
