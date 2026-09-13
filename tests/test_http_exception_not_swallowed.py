"""HTTPException внутри try не должна глушиться широким except.

Класс бага, найденный на живом стенде дважды (2026-09-13):
* `/cypher`: пустой запрос отдавал HTTP 200 с полем error в теле;
* `/process`: новый 409 «уже в очереди» превращался в 500.

Причина одна: `raise HTTPException(...)` стоит ВНУТРИ `try`, а первым идёт
`except Exception` — он её ловит. Тест сканирует исходники роутов синтаксическим
деревом, поэтому ловит любой новый такой случай, а не только известные.
"""
import ast
from pathlib import Path

ROUTES = sorted((Path(__file__).resolve().parents[1] / "src/api/routes").glob("*.py"))


def _chains(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for handler in node.body:
            if not isinstance(handler, ast.Try):
                continue
            caught = [ast.unparse(e.type) for e in handler.handlers if e.type]
            broad = [c for c in caught if "HTTPException" not in c and "Exception" in c]
            if not broad:
                continue
            raises_http = any(
                isinstance(n, ast.Raise) and n.exc is not None and "HTTPException" in ast.unparse(n)
                for n in ast.walk(handler)
            )
            if not raises_http:
                continue
            first_http = next((i for i, e in enumerate(handler.handlers)
                               if "HTTPException" in ast.unparse(e.type)), None)
            first_broad = next(i for i, e in enumerate(handler.handlers)
                               if "HTTPException" not in ast.unparse(e.type)
                               and "Exception" in ast.unparse(e.type))
            yield path.name, node.name, handler.lineno, first_http, first_broad


def test_no_route_swallows_http_exception():
    bad = []
    for path in ROUTES:
        for name, func, lineno, first_http, first_broad in _chains(path):
            if first_http is None or first_http > first_broad:
                bad.append(f"{name}:{func}() строка {lineno}")
    assert not bad, (
        "HTTPException внутри try попадает в широкий except (станет 500 или 200 с error); "
        "нужен `except HTTPException: raise` ПЕРВЫМ обработчиком:\n  " + "\n  ".join(bad)
    )


def test_known_routes_keep_explicit_passthrough():
    """Точечная страховка на места, где это уже ломалось."""
    checks = {
        "knowledge_graph.py": "async def execute_cypher",
        "upload.py": "async def process_document_now",
    }
    for fname, func in checks.items():
        src = (Path(__file__).resolve().parents[1] / "src/api/routes" / fname).read_text(encoding="utf-8")
        start = src.index(func)
        block = src[start:start + 3000]
        assert "except HTTPException:" in block, f"{fname}:{func} — потерян проброс HTTPException"
