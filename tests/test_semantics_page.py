"""Страница «Семантика»: живое состояние словарей и схема графовой базы.

Страница показывает то, что нельзя проверить юнитом (вёрстку и живые числа), поэтому тесты
держат КАРКАС: (1) роут закрыт админом, (2) на странице есть все три оси и оба предупреждения о
мягкости, (3) страница не держит собственных списков словарей (берёт их с сервера), (4) эндпоинт
отдаёт значения словарей и числа, а не флаги.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "src/api/main.py").read_text(encoding="utf-8")
PAGE = (ROOT / "src/api/static/semantics.html").read_text(encoding="utf-8")
ADMIN = (ROOT / "src/api/routes/admin_models.py").read_text(encoding="utf-8")
NAV = (ROOT / "src/api/static/site-nav.js").read_text(encoding="utf-8")


def test_страница_только_для_админа():
    start = MAIN.index('@app.get("/semantics"')
    block = MAIN[start:start + 900]
    assert "_is_admin_request(request)" in block, "страница обязана проверять админа"
    assert 'RedirectResponse(url="/documents", status_code=302)' in block
    assert "_html_response" in block, "HTML отдаём no-store хелпером, иначе браузер кэширует"


def test_страница_в_админском_меню():
    assert "['/semantics', 'Семантика']" in NAV
    assert "role: 'admin'" in NAV


def test_на_странице_три_оси_и_мягкость():
    """Схема рисуется Graphviz из текста (axes.dot) и лежит готовым SVG: проверяем и текст
    источника, и то, что страница ссылается на картинку."""
    dot = (ROOT / "scripts/diagrams/axes.dot").read_text(encoding="utf-8")
    for needle in ("Вид", "kinds", "Тема", "rubrics", "Фасет", "facets", "МЯГКО", "мягко"):
        assert needle in dot, f"на схеме нет «{needle}»"
    assert "/static/diagrams/axes.svg" in PAGE, "страница обязана показывать схему осей"
    assert "не ограничивают поиск" in PAGE, "на странице должно быть сказано про мягкость осей"


def test_схемы_лежат_готовыми_файлами():
    """Тулчейн (Graphviz) в поставку не тащим: на странице — готовый SVG из репозитория."""
    for name in ("axes.svg", "pipeline.svg"):
        assert (ROOT / "src/api/static/diagrams" / name).exists(), f"нет {name}"
    for name in ("axes.dot", "pipeline.dot"):
        assert (ROOT / "scripts/diagrams" / name).exists(), f"нет источника {name}"


def test_на_странице_показан_конвейер_графовой_базы():
    """Человек должен видеть, как строится база: шаги и чем каждый проверить."""
    for step in ("Файл принят", "Происхождение", "Разбор содержимого", "Фрагменты",
                 "Разметка осей", "Векторы в коллекцию по маршруту", "Граф: сущности и связи",
                 "Ручные правки"):
        assert step in PAGE, f"нет шага «{step}»"


def test_страница_берёт_данные_с_сервера_а_не_держит_своих_списков():
    assert "'/admin/models/semantics-state'" in PAGE
    assert "setInterval(load" in PAGE, "состояние должно обновляться само (режим реального времени)"
    # списков кодов в HTML быть не должно: они приходят с сервера
    assert "national_standard" not in PAGE and "infosec" not in PAGE


def test_эндпоинт_отдаёт_значения_а_не_флаги():
    start = ADMIN.index('@router.get("/semantics-state"')
    body = ADMIN[start:start + 4000]
    assert '"unmarked"' in body, "должно быть видно, сколько документов НЕ размечено"
    assert "document_kinds.as_list()" in body and "document_topics.as_list()" in body \
        and "document_facets.as_list()" in body, "словари берём из единых источников"
    assert "documents" in body and "vectors_main" in body and "get_stats" in body, \
        "состояние хранилищ (векторы и граф) обязано попадать в ответ"
