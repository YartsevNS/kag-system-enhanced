"""Словарь видов документов v0 — единый источник, маршрут коллекции, миграция.

Что стерегут эти тесты:
  * список видов живёт в ОДНОМ месте, а перечисление анализатора и страницы с ним не расходятся;
  * вид документа больше НЕ решает, в какую коллекцию писать векторы (это делал `type == 'news'`,
    из-за чего смена словаря тихо переносила 27 новостей ЦБ из kag_news в общую коллекцию);
  * миграция старого набора значений в словарь v0 не выдумывает вид там, где соответствие неясно.
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


migrate = _load_module("scripts/bakeoff/migrate_document_types.py", "migrate_document_types")

from src.indexing import document_kinds as dk  # noqa: E402
from src.indexing.auto_tagger import DocumentType  # noqa: E402
from src.indexing.embeddings_service import (  # noqa: E402
    NEWS_COLLECTION, collection_for_source, is_news_document,
)
from src.api.services.document_analyzer import document_analyzer as analyzer  # noqa: E402


# ── Словарь: структура ────────────────────────────────────────────────────────

def test_словарь_не_пустой_и_в_пределах_порога():
    assert 20 <= len(dk.KIND_CODES) <= 25, "владелец держит порог «не более 25 видов»"
    assert len(set(dk.KIND_CODES)) == len(dk.KIND_CODES), "коды видов уникальны"


def test_у_каждого_вида_есть_подписи_группа_и_определение():
    for code, meta in dk.KINDS.items():
        assert meta.get("title"), f"{code}: нет человеческого названия"
        assert meta.get("short"), f"{code}: нет короткой подписи для плашки"
        assert meta.get("group") in dk.GROUPS, f"{code}: группа «{meta.get('group')}» не из GROUPS"
        assert len(meta.get("definition") or "") > 20, f"{code}: определение слишком короткое"


def test_новости_не_вид_документа():
    """«Новость» — свойство источника (маршрут коллекции), а не вид. Раньше это был вид `news`."""
    assert "news" not in dk.KIND_CODES
    assert "publication" in dk.KIND_CODES


def test_принятые_владельцем_виды_на_месте():
    """ПНСТ добавлен при утверждении, общероссийский классификатор — нет (в корпусе отсутствует)."""
    assert "preliminary_standard" in dk.KIND_CODES
    assert "classifier" not in dk.KIND_CODES


# ── Один источник: перечисление анализатора == словарь ───────────────────────

def test_перечисление_анализатора_совпадает_со_словарём():
    assert {t.value for t in DocumentType} == set(dk.KIND_CODES)


def test_строка_для_промпта_содержит_все_виды():
    line = dk.vocabulary_line()
    for code in dk.KIND_CODES:
        assert code in line


def test_неизвестный_код_не_считается_видом_но_подписывается_как_есть():
    assert dk.is_valid("national_standard") is True
    assert dk.is_valid("standard") is False
    assert dk.is_valid("") is False and dk.is_valid(None) is False
    # Старое значение (до миграции) показываем кодом: пустая плашка хуже честного кода
    assert dk.short("standard") == "standard"
    assert dk.title("news") == "news"


# ── Анализатор документов: пишем только коды словаря ─────────────────────────

def test_анализатор_принимает_код_словаря():
    res = analyzer._parse_response('{"title": "ГОСТ Р 1", "type": "national_standard"}', "f.pdf")
    assert res["document_type"] == "national_standard"


def test_анализатор_отвергает_старые_значения():
    for old in ("standard", "news", "legal", "order", "financial"):
        res = analyzer._parse_response('{"title": "T", "type": "%s"}' % old, "f.pdf")
        assert "document_type" not in res, f"старое значение «{old}» не должно попадать в базу"


# ── Маршрут коллекции: явный признак, а не вид ───────────────────────────────

def test_явный_признак_новостей():
    assert is_news_document({"collection": "news"}) is True
    assert is_news_document({"collection": "documents"}) is False


def test_явный_отказ_сильнее_метаданных_источника():
    """Документ с метаданными источника можно осознанно оставить в основной коллекции."""
    doc = {"collection": "documents", "source_metadata": {"source_url": "http://x/rss"}}
    assert is_news_document(doc) is False


def test_признак_пуст_решают_метаданные_источника():
    assert is_news_document({"source_metadata": {"source_name": "ЦБ РФ"}}) is True
    assert is_news_document({"source_metadata": {}}) is False
    assert is_news_document({}) is False


def test_вид_документа_больше_не_маршрут():
    """ГЛАВНОЕ: даже с видом «новость» (старое значение) документ без признака идёт в основную."""
    assert is_news_document({"document_type": "news"}) is False
    assert is_news_document({"document_type": "publication"}) is False


def test_коллекция_для_нового_документа_по_источнику():
    assert collection_for_source({"source_url": "http://x"}) == NEWS_COLLECTION
    assert collection_for_source({"source_id": 7}) == NEWS_COLLECTION
    assert collection_for_source(None) == ""
    assert collection_for_source({}) == ""


def test_поле_collection_есть_в_модели_и_миграциях():
    model = (ROOT / "src/database/document_models.py").read_text(encoding="utf-8")
    migr = (ROOT / "src/database/migrations.py").read_text(encoding="utf-8")
    assert re.search(r'^\s*collection = Column', model, re.M)
    assert '"documents", "collection"' in migr


def test_загрузка_документа_ставит_маршрут_по_источнику():
    src = (ROOT / "src/api/services/document_service.py").read_text(encoding="utf-8")
    assert "collection=collection_for_source(source_metadata)" in src
    # коллекция обязана быть в списке полей, которые не затираются пустыми значениями
    assert '"source_metadata", "collection"' in src


# ── Миграция старого набора значений ─────────────────────────────────────────

def test_миграция_простых_значений():
    assert migrate.target_kind("contract", "")[0] == "contract"
    assert migrate.target_kind("order", "")[0] == "subordinate_act"
    assert migrate.target_kind("legal", "")[0] == "law"
    assert migrate.target_kind("policy", "")[0] == "regulation"
    assert migrate.target_kind("letter", "")[0] == "official_letter"


def test_миграция_стандартов_по_номеру_в_заголовке():
    assert migrate.target_kind("standard", "ГОСТ Р 34.10—2012. Криптографическая защита")[0] == \
        "national_standard"
    assert migrate.target_kind("standard", "Р 1323565.1.004—2017. Рекомендации по стандартизации")[0] == \
        "standardization_recommendation"
    assert migrate.target_kind("standard", "Рекомендации по стандартизации Р 50.1.113—2016")[0] == \
        "standardization_recommendation"
    assert migrate.target_kind("standard", "Стандарт Банка России СТО БР БФБО-1.9-2024")[0] == \
        "org_standard"
    assert migrate.target_kind("standard", "ПНСТ 123-2020")[0] == "preliminary_standard"


def test_миграция_не_выдумывает_вид():
    """Заголовок с упоминанием ГОСТ, но не стандарт, и неизвестное значение — на ревью."""
    new, why = migrate.target_kind("standard", "Требования к защите информации в соответствии с ГОСТ Р 56545")
    assert new is None and why
    new, why = migrate.target_kind("неведомое", "что-то")
    assert new is None and "не описано" in why


def test_миграция_новостей_закрепляет_маршрут():
    """news → publication, и ОБЯЗАТЕЛЬНО явный маршрут коллекции — иначе путь векторов изменится."""
    new, _ = migrate.target_kind("news", "Банк России публикует Резюме обсуждения ключевой ставки")
    assert new == "publication"
    assert migrate.COLLECTION_MARK.get("news") == NEWS_COLLECTION


def test_миграция_финансовых_разделяет_квитанцию_и_справку():
    assert migrate.target_kind("financial", "Квитанция-извещение на оплату ЖКУ")[0] == "invoice"
    assert migrate.target_kind("financial", "Дашборды по процентным ставкам")[0] == "reference"


# ── Страницы берут список видов из системы, а не держат свой ─────────────────

def test_страницы_берут_виды_из_единого_источника():
    docs = (ROOT / "src/api/static/documents.html").read_text(encoding="utf-8")
    viewer = (ROOT / "src/api/static/viewer.html").read_text(encoding="utf-8")
    for page in (docs, viewer):
        assert "/document-kinds" in page
        assert "'Новость'" not in page and '"Новость"' not in page, "старые подписи видов вернулись"


def test_фильтр_видов_пересобирается_а_не_дополняется():
    """Гонка двух запросов: документы приходят раньше словаря, и «дополнение» списка оставляло
    код вида вместо подписи (в фильтре навсегда `national_standard` вместо «ГОСТ»). Живая
    проверка в Chromium 10.10.2026 поймала это на втором прогоне — тест стережёт возврат к
    дополнению списка."""
    page = (ROOT / "src/api/static/documents.html").read_text(encoding="utf-8")
    assert "while (sel.options.length > 1) sel.remove(1)" in page, \
        "список видов обязан пересобираться целиком, а не дополняться"


def test_роут_словаря_зарегистрирован():
    main = (ROOT / "src/api/main.py").read_text(encoding="utf-8")
    assert "meta.router" in main
    from src.api.routes import meta  # noqa: PLC0415 — проверяем загружаемость модуля
    assert any(getattr(r, "path", "") == "/document-kinds" for r in meta.router.routes)


def test_админский_список_видов_отдаёт_словарь():
    src = (ROOT / "src/api/routes/admin_models.py").read_text(encoding="utf-8")
    i = src.index('@router.get("/doc-types"')
    body = src[i:i + 2500]
    assert "document_kinds" in body, "админский список обязан опираться на единый словарь"
