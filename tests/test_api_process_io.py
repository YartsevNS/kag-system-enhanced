"""Файлы, работающие В ПРОЦЕССЕ API: ФС и SQL не должны блокировать loop.

Отдельный тест, потому что это не роуты upload/chat, а инфраструктура процесса:
HotFolderWatcher стартует в lifespan, страницы отдаются через _html_response, а в
watchers.py есть и синхронные обработчики (им to_thread не нужен — FastAPI сам
исполняет их в пуле потоков).
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_hot_folder_watcher_does_not_block_loop():
    """ФС-вызовы допустимы только в синхронных хелперах, а не в async-цикле.

    «iterdir внутри файла» — не признак проблемы: правильный вариант как раз в том,
    что он вынесен в синхронный _list_hot_files и вызывается через to_thread.
    Поэтому проверяем по AST, что в async-функциях блокирующих вызовов нет.
    """
    src = _src("src/indexing/hot_folder_watcher.py")
    assert "asyncio.to_thread" in src, "HotFolderWatcher работает в процессе API — ФС должна идти в поток"
    tree = ast.parse(src)
    blocking = ("iterdir", "rglob", "glob", "open", "stat", "exists", "mkdir")
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = ast.unparse(sub.func)
                if any(f".{b}" in f".{name}" for b in blocking) and "to_thread" not in ast.unparse(node):
                    bad.append(f"{node.name}() → {name}")
    assert not bad, f"синхронный ФС-вызов в async: {bad}"


def test_html_response_is_async_and_wrapped():
    src = _src("src/api/main.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "_html_response")
    assert isinstance(fn, ast.AsyncFunctionDef), "проверка существования файла должна быть в потоке"
    assert "asyncio.to_thread" in ast.unparse(fn), "os.path.exists снова синхронный"
    assert "return await _html_response(" in src, "вызовы _html_response должны быть awaited"
    assert "return _html_response(" not in src, "остался вызов без await"


def test_watchers_only_async_route_uses_to_thread():
    src = _src("src/api/routes/watchers.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and "db.query(" in ast.unparse(sub.func):
                assert "asyncio.to_thread" in ast.unparse(node), (
                    f"{node.name}: синхронный db.query в async-обработчике"
                )
    # синхронные обработчики трогать не нужно: FastAPI зовёт их в пуле потоков
    assert "async def remove_watched_url" not in src, (
        "remove_watched_url был синхронным — ему to_thread не нужен"
    )
