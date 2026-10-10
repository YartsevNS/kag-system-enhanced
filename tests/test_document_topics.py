"""Темы (рубрики) документа — словарь v0, многозначность, МЯГКОСТЬ (пункт A4 дорожной карты).

Главный страж здесь — не структура словаря, а ПРАВИЛО: тема не ограничивает поиск по умолчанию.
Поэтому тесты (а) проверяют, что в коде нет жёсткого фильтра по ключу `rubrics`, и (б) что тема
приходит в данные списком (многозначность) — то, из-за отсутствия чего тема раньше не фильтровалась
вовсе.
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


migrate = _load_module("scripts/bakeoff/migrate_domain_to_rubrics.py", "migrate_domain_to_rubrics")

from src.indexing import document_topics as dt  # noqa: E402
from src.api.services.document_analyzer import document_analyzer as analyzer  # noqa: E402


# ── Словарь тем ───────────────────────────────────────────────────────────────

def test_словарь_тем_непустой_и_уникальный():
    assert len(dt.RUBRIC_CODES) >= 5
    assert len(set(dt.RUBRIC_CODES)) == len(dt.RUBRIC_CODES)


def test_у_каждой_темы_есть_название_и_граница():
    for code, meta in dt.RUBRICS.items():
        assert meta.get("title"), f"{code}: нет человеческого названия"
        assert len(meta.get("definition") or "") > 20, f"{code}: определение слишком короткое"


def test_информационная_безопасность_в_словаре():
    """Та самая ось, которой не хватало: тема, а не вид документа."""
    assert dt.is_valid("infosec")
    assert "Информационная" in dt.title("infosec")


def test_неизвестная_тема_не_выдаётся_за_известную():
    assert dt.is_valid("cyber") is False
    assert dt.title("cyber") == "cyber", "неизвестный код показываем как есть, а не выдумываем"


# ── Многозначность и приведение к словарю ─────────────────────────────────────

def test_несколько_тем_сохраняются():
    assert dt.normalize(["infosec", "law"]) == ["infosec", "law"]


def test_незнакомые_коды_отбрасываются_а_не_складываются_в_other():
    """«Неизвестный код» и «тема не определена» — разные вещи: смешать значит потерять ошибку."""
    assert dt.normalize(["infosec", "чепуха"]) == ["infosec"]
    assert dt.normalize(["чепуха"]) == []
    assert dt.normalize([]) == [] and dt.normalize(None) == []


def test_дубли_убираются_а_порядок_берётся_из_словаря():
    assert dt.normalize(["law", "infosec", "law"]) == ["infosec", "law"]


def test_строка_принимается_как_одна_тема():
    assert dt.normalize("infosec") == ["infosec"]


# ── Анализатор: темы приходят списком и проверяются по словарю ────────────────

def test_анализатор_принимает_несколько_тем():
    res = analyzer._parse_response(
        '{"title": "ГОСТ", "type": "national_standard", "rubrics": ["infosec", "law"]}', "f.pdf")
    assert res["rubrics"] == ["infosec", "law"]


def test_анализатор_отбрасывает_выдуманные_темы():
    res = analyzer._parse_response('{"title": "T", "rubrics": ["кибербезопасность"]}', "f.pdf")
    assert "rubrics" not in res, "тема вне словаря не должна попадать в данные"


def test_в_промпте_есть_список_тем():
    prompt = analyzer._build_prompt("текст", "f.pdf")
    for code in dt.RUBRIC_CODES:
        assert code in prompt, f"код {code} не попал в промпт — модель его не выберет"


# ── МЯГКОСТЬ: тема не ограничивает поиск ─────────────────────────────────────

def test_нет_жёсткого_фильтра_по_темам_кроме_особого_режима():
    """Правило A4: по умолчанию тема поиск НЕ ограничивает.

    Фильтр по полю тем существует ровно в одном месте (`_theme_condition` в embeddings_service),
    и мягкий режим в нём допускает документы без темы. Жёсткий вариант включается только осознанной
    настройкой `chat/domain=hard` (тест режима — в test_domain_filter_modes.py). Любое ДРУГОЕ место,
    где код отсекает по темам, ломает правило — такой файл здесь и палится.
    """
    offenders = []
    for path in ROOT.joinpath("src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if 'key="rubrics"' in text and path.name != "embeddings_service.py":
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "фильтровать по темам можно только в _theme_condition, а не здесь: " + \
        ", ".join(offenders)
    body = (ROOT / "src/indexing/embeddings_service.py").read_text(encoding="utf-8")
    soft = body.split("def _theme_condition")[1].split("def _legacy_domain_condition")[0]
    assert "should=[" in soft and 'key="rubrics"' in soft, "мягкий режим обязан быть в условии темы"


def test_мягкость_видна_в_условии_темы():
    """Мягкий режим темы обязан остаться: документы без темы не отсекаются."""
    emb = (ROOT / "src/indexing/embeddings_service.py").read_text(encoding="utf-8")
    assert "theme_include_empty" in emb, "мягкий режим темы не должен потеряться"


# ── Данные: список, сериализация, payload ────────────────────────────────────

def test_rubrics_это_список_в_модели_и_в_json_полях():
    model = (ROOT / "src/database/document_models.py").read_text(encoding="utf-8")
    repo = (ROOT / "src/api/services/document_repository.py").read_text(encoding="utf-8")
    assert re.search(r'^\s*rubrics = Column', model, re.M)
    assert '"rubrics"' in repo, "rubrics обязан быть в списке JSON-полей, иначе в PG уедет array"


def test_колонка_добавлена_миграцией():
    migr = (ROOT / "src/database/migrations.py").read_text(encoding="utf-8")
    assert '"documents", "rubrics"' in migr


def test_rubrics_уходят_в_payload_qdrant():
    src = (ROOT / "src/api/services/document_service.py").read_text(encoding="utf-8")
    # Тема в payload — настоящим списком (не JSON-строкой): фильтр по строке не работает,
    # а ручная правка кладёт туда именно список.
    assert '"rubrics": _as_payload_json' in src, "тема обязана попадать в payload (витрины, выгрузки)"
    assert '"topics", "rubrics", "document_type"' in src, \
        "rubrics обязан быть в списке полей, которые не затираются пустыми значениями"


def test_страница_фильтрует_по_темам_мягко():
    page = (ROOT / "src/api/static/documents.html").read_text(encoding="utf-8")
    assert 'id="rubric-filter"' in page
    assert "d.rubrics || []" in page
    assert "НЕ ограничивает поиск" in page, "в подсказке фильтра должно быть сказано, что признак мягкий"


# ── Миграция легаси-доменов ──────────────────────────────────────────────────

def test_домены_переносятся_в_темы():
    assert migrate.DOMAIN_TO_RUBRIC["infosec"] == "infosec"
    assert migrate.DOMAIN_TO_RUBRIC["legal"] == "law"
    assert migrate.DOMAIN_TO_RUBRIC["accounting"] == "economics"


def test_универсальный_домен_это_отсутствие_темы():
    """«universal» — не тема: писать его рубрикой значит выдумать содержание."""
    assert migrate.DOMAIN_TO_RUBRIC["universal"] == ""


def test_миграция_не_угадывает_незнакомый_домен():
    assert "военное_дело" not in migrate.DOMAIN_TO_RUBRIC
