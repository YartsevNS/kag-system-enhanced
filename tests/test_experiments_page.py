"""Страница «Опыты и модели»: роут только для админа, данные — из config_store.

Зачем тест: страница задумана админской (результаты замеров и планы по GPU). Проверяем
по исходнику три вещи, которые легко потерять: (1) роут закрыт админской проверкой и
редиректит остальных; (2) данные читаются/пишутся в config_store через отдельный
эндпоинт, поэтому обновляются без пересборки образа; (3) ссылка в навигации появляется
в branding.js только у админа.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "src/api/main.py").read_text(encoding="utf-8")
ADMIN = (ROOT / "src/api/routes/admin_models.py").read_text(encoding="utf-8")
PAGE = (ROOT / "src/api/static/experiments.html").read_text(encoding="utf-8")
BRANDING = (ROOT / "src/api/static/branding.js").read_text(encoding="utf-8")


def test_page_route_is_admin_only():
    start = MAIN.index('@app.get("/experiments"')
    block = MAIN[start:start + 900]
    assert "_is_admin_request(request)" in block, "страница обязана проверять админа"
    assert 'RedirectResponse(url="/documents", status_code=302)' in block, (
        "не-админа уводим на документы (как у /admin)"
    )
    assert "_html_response" in block, "HTML отдаём через no-store хелпер, иначе браузер кэширует"


def test_page_reads_data_from_admin_api():
    # URL собирается из константы API, поэтому проверяем обе части
    assert "const API = '/api/v1'" in PAGE, "страница должна знать базовый путь API"
    assert "'/admin/models/experiments'" in PAGE, "страница берёт данные из админского эндпоинта"
    assert "render(FALLBACK)" in PAGE, "при отсутствии данных должна работать встроенная выборка"
    assert "Ограничения замера" in PAGE, "на странице обязательна оговорка про смещение пулов"


def test_endpoints_store_data_in_config_store():
    assert '@router.get("/experiments"' in ADMIN and '@router.post("/experiments"' in ADMIN
    assert '"experiments", "models"' in ADMIN, "данные страницы живут в config_store"
    block = ADMIN[ADMIN.index('@router.post("/experiments"'):]
    assert "model_dump(exclude_unset=True)" in block[:600], "тело — Pydantic, без data: dict"
    assert "class ExperimentsUpdate(BaseModel)" in ADMIN
    # раздел «железо» рисуется из тех же данных, поэтому поле должно быть в схеме
    assert "hardware: Optional[List[Dict[str, Any]]]" in ADMIN
    assert "id=\"hw-table\"" in PAGE and "d.hardware" in PAGE


def test_nav_link_visible_only_to_admin():
    assert "/api/v1/auth/me" in BRANDING and "u.is_admin" in BRANDING, (
        "ссылку добавляем только админу, проверяя /auth/me"
    )
    assert 'a.href = \'/experiments\'' in BRANDING
