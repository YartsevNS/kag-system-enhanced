"""Структурные проверки роутов /kg — ловят регрессии, которые видно только живьём.

1) Имена сущностей содержат слэш («ГОСТ Р ИСО/МЭК 15408-2-2013») — пути
   /graph/{name} и /entity/{name}/chunks обязаны использовать конвертер :path,
   иначе FastAPI отдаёт 404 (проверено на стенде 2026-09-13).
2) /cypher: широкий except возвращал HTTP 200 с полем error, а собственная
   HTTPException(400) глушилась. Порядок обработчиков и коды ответов
   зафиксированы здесь по исходнику.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src/api/routes/knowledge_graph.py"
TEXT = SRC.read_text(encoding="utf-8")


def test_entity_routes_use_path_converter():
    assert '@router.get("/graph/{entity_name:path}"' in TEXT, (
        "роут графа должен принимать имена со слэшем"
    )
    assert '@router.get("/entity/{entity_name:path}/chunks"' in TEXT, (
        "роут фрагментов сущности должен принимать имена со слэшем"
    )


def test_cypher_passes_http_exceptions_through():
    start = TEXT.index("async def execute_cypher")
    block = TEXT[start:start + 1600]
    assert "except HTTPException:" in block, "HTTPException должна пробрасываться, а не глушиться"
    assert "raise HTTPException(status_code=400" in block, "ошибка запроса — это 4xx"
    assert "raise HTTPException(status_code=502" in block, "сбой Neo4j — это 5xx"
    assert 'return {"query": query.get("query"), "results": [], "error"' not in block, (
        "ошибка не должна возвращаться с кодом 200"
    )
    # порядок: проброс HTTPException идёт раньше широкого except Exception
    assert block.index("except HTTPException:") < block.index("except Exception as e:")


def test_cypher_logs_successful_query():
    block = TEXT[TEXT.index("async def execute_cypher"):TEXT.index("async def execute_cypher") + 1600]
    assert "[cypher]" in block, "успешный произвольный запрос должен попадать в лог с пользователем"
