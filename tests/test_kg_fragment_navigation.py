"""Переход из /kg к фрагменту и странице: контракт фронта и бэкенда.

Зачем тест: связка «клик по узлу → открыть файл на странице фрагмента и подсветить
найденное» состоит из четырёх частей в четырёх файлах, и любая может отвалиться
молча (страница отдаётся с кодом 200 при любой ошибке в JS):

1) бэкенд отдаёт у узлов-фрагментов id точки Qdrant (`qdrant_point_id`), иначе
   номер страницы взять неоткуда — в Neo4j страницы нет;
2) роут графа добирает `page` из payload'ов Qdrant (`_attach_pages`) — иначе
   файл открывается с первой страницы;
3) /kg даёт НАСТОЯЩУЮ ссылку на просмотрщик (window.open глушится блокировкой
   всплывающих окон) и передаёт страницу + термины запроса (`&page=`, `&q=`);
4) просмотрщик читает `q` и подсвечивает совпадения в текстовом слое.

Проверки по исходнику — как в tests/test_kg_route_guards.py: поведение тут не
проверить без стенда, а контракт зафиксировать нужно.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "src/api/routes/knowledge_graph.py").read_text(encoding="utf-8")
KG_SERVICE = (ROOT / "src/indexing/knowledge_graph.py").read_text(encoding="utf-8")
KG_PAGE = (ROOT / "src/api/static/kg.html").read_text(encoding="utf-8")
VIEWER = (ROOT / "src/api/static/viewer.html").read_text(encoding="utf-8")


def test_graph_chunk_nodes_expose_qdrant_point_id():
    """Без id точки Qdrant номер страницы не добрать (в Neo4j его нет)."""
    search_block = KG_SERVICE[KG_SERVICE.index("def search_chunks("):]
    search_block = search_block[:search_block.index("def get_document_graph(")]
    assert '"qdrant_point_id": c["pid"]' in search_block, (
        "узлы-фрагменты поиска по тексту должны нести qdrant_point_id"
    )

    doc_block = KG_SERVICE[KG_SERVICE.index("def get_document_graph("):]
    doc_block = doc_block[:doc_block.index("def chunk_info(")]
    assert "coalesce(c.qdrant_point_id, '') AS pid" in doc_block, (
        "подграф документа должен забирать qdrant_point_id чанка"
    )
    assert '"qdrant_point_id": c["pid"]' in doc_block, (
        "узлы-фрагменты подграфа документа должны нести qdrant_point_id"
    )


def test_routes_attach_page_to_chunk_nodes():
    assert "async def _attach_pages(" in ROUTES, "хелпер обогащения страницами пропал"
    # один запрос payload'ов на весь граф, ошибка Qdrant не ломает граф
    helper = ROUTES[ROUTES.index("async def _attach_pages("):]
    helper = helper[:helper.index("@router.get(\"/entity/")]
    assert "get_points_payload" in helper
    assert "except Exception as e:" in helper, "сбой Qdrant не должен ломать граф"

    for route in ("async def entity_graph(", "async def search_chunks(", "async def document_graph("):
        block = ROUTES[ROUTES.index(route):]
        block = block[:block.index("@router.")] if "@router." in block else block
        assert "_attach_pages(" in block, f"{route}: узлам графа не проставлена страница"


def test_kg_links_open_viewer_with_page_and_query():
    assert "function docHref(" in KG_PAGE and "'&page='" in KG_PAGE, "ссылка должна нести номер страницы"
    assert "'&q='" in KG_PAGE, "ссылка должна нести термины запроса (для подсветки в просмотрщике)"

    doc_link = KG_PAGE[KG_PAGE.index("function docLink("):]
    doc_link = doc_link[:doc_link.index("function chunksLink(")]
    assert 'target="_blank"' in doc_link and "<a class=" in doc_link, (
        "переход к файлу — настоящая ссылка, а не window.open (всплывающие окна глушатся)"
    )

    # старый путь через window.open из панели убран
    assert 'onclick="openDoc(' not in KG_PAGE, "остались кнопки с window.open"


def test_kg_marks_fragment_that_contains_the_answer():
    # термины поиска подсвечиваются в тексте фрагмента (безопасно: сначала экранирование)
    assert "function markTerms(" in KG_PAGE and 'class="kg-hit"' in KG_PAGE
    body = KG_PAGE[KG_PAGE.index("function markTerms("):]
    body = body[:body.index("function matchedChunkIds(")]
    assert "esc(String(text" in body, "подсветка обязана идти после экранирования"

    # фрагменты-совпадения идут первыми и помечены
    panel = KG_PAGE[KG_PAGE.index("async function loadEntityChunks("):]
    panel = panel[:panel.index("async function loadChunkText(")]
    assert "matchedChunkIds()" in panel, "панель сущности должна знать совпадения текущего графа"
    assert "kg-badge-hit" in panel, "фрагмент-совпадение должен быть помечен"
    assert "rows.sort(" in panel, "совпадения должны идти первыми, а не в порядке номера"

    # узел-документ открывается на странице найденного фрагмента, а не с первой
    assert "function firstDocPage(" in KG_PAGE
    assert "firstDocPage(d.document_id)" in KG_PAGE


def test_viewer_highlights_query_terms():
    assert "params.get('q')" in VIEWER, "просмотрщик должен читать термины запроса"
    assert "function applyHighlight(" in VIEWER and "hl-term" in VIEWER
    # слой рисуется асинхронно — подсветку нельзя await'ить (иначе вьюер зависает)
    sched = VIEWER[VIEWER.index("function scheduleHighlight("):]
    sched = sched[:sched.index("function applyHighlight(")]
    assert "setTimeout" in sched, "подсветка должна дожидаться спанов без блокировки страницы"
    assert "await" not in sched
    # номер страницы из URL по-прежнему открывается сразу
    assert "params.get('page')" in VIEWER and "renderPage(initPage)" in VIEWER


def test_kg_nodes_carry_page_into_graph_data():
    """Страница фрагмента должна доехать до данных узла, иначе ссылку не построить."""
    block = KG_PAGE[KG_PAGE.index("function addGraphData("):]
    block = block[:block.index("async function loadGraphFromInput(")]
    assert re.search(r"page:\s*\(n\.page", block), "узел графа должен получать page из ответа API"
