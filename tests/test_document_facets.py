"""Фасеты документа — закрытые перечни значений (пункт A3 дорожной карты).

Страж здесь двойной: (1) значения вне перечня НЕ пишутся и это видно, (2) в реестре нет фасетов,
под которые ещё нет кода (иначе получается «настройка есть, кода нет» — дефект, который уже
вычищали в других местах).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from src.indexing import document_facets as df  # noqa: E402
from src.api.services.document_analyzer import document_analyzer as analyzer  # noqa: E402


# ── Реестр фасетов ────────────────────────────────────────────────────────────

def test_реестр_содержит_только_реализованные_фасеты():
    """stage/status/гриф описаны в черновике словаря, но разметки под них нет — в реестре их быть
    не должно, пока их нечем заполнять."""
    assert set(df.FACET_CODES) == {"protection_subject", "normative_force"}


def test_у_фасетов_есть_названия_и_закрытые_перечни():
    for code, meta in df.FACETS.items():
        assert meta.get("title"), f"{code}: нет названия"
        assert len(meta.get("values") or {}) >= 3, f"{code}: перечень слишком короткий"
        for value, desc in (meta.get("values") or {}).items():
            assert desc, f"{code}.{value}: нет описания значения"


def test_значения_вне_перечня_не_считаются_валидными():
    assert df.is_valid_value("protection_subject", "data") is True
    assert df.is_valid_value("protection_subject", "котики") is False
    assert df.is_valid_value("нет_такого_фасета", "data") is False
    assert df.is_valid_facet("protection_subject") is True
    assert df.is_valid_facet("этап") is False


# ── Многозначность и закрытость ──────────────────────────────────────────────

def test_предмет_защиты_многозначен():
    assert df.normalize({"protection_subject": ["data", "networks"]}) == \
        {"protection_subject": ["data", "networks"]}


def test_незнакомые_значения_и_фасеты_отбрасываются():
    out = df.normalize({"protection_subject": ["data", "чепуха"], "этап": ["проектирование"]})
    assert out == {"protection_subject": ["data"]}
    assert df.normalize({"protection_subject": ["чепуха"]}) == {}
    assert df.normalize(None) == {} and df.normalize("строка") == {}


def test_нормативная_сила_принимает_одно_значение():
    assert df.normalize({"normative_force": "mandatory"}) == {"normative_force": ["mandatory"]}


def test_не_применимо_это_допустимое_значение():
    """«Документ не про защиту» — законный ответ, а не отсутствие разметки."""
    assert df.normalize({"protection_subject": ["not_applicable"]}) == \
        {"protection_subject": ["not_applicable"]}


# ── Анализатор: фасеты приходят по закрытым перечням ─────────────────────────

def test_анализатор_принимает_фасеты_из_перечня():
    res = analyzer._parse_response(
        '{"title": "ГОСТ", "facets": {"protection_subject": ["data", "networks"], '
        '"normative_force": "mandatory"}}', "f.pdf")
    assert res["facets"]["protection_subject"] == ["data", "networks"]
    assert res["facets"]["normative_force"] == ["mandatory"]


def test_анализатор_отбрасывает_значения_вне_перечня():
    res = analyzer._parse_response(
        '{"title": "T", "facets": {"protection_subject": ["котики"]}}', "f.pdf")
    assert "facets" not in res, "значение вне закрытого перечня не должно попадать в данные"


def test_в_промпте_есть_фасеты_и_их_значения():
    prompt = analyzer._build_prompt("текст", "f.pdf")
    for code in df.FACET_CODES:
        assert code in prompt
    for value in df.FACETS["protection_subject"]["values"]:
        assert value in prompt, f"значение {value} не попало в промпт"


# ── Запись: база, payload, ручная правка ─────────────────────────────────────

def test_facets_хранятся_как_json_и_уезжают_в_payload_объектом():
    repo = (ROOT / "src/api/services/document_repository.py").read_text(encoding="utf-8")
    svc = (ROOT / "src/api/services/document_service.py").read_text(encoding="utf-8")
    assert '"facets"' in repo, "facets обязан быть в списке JSON-полей (иначе dict не запишется)"
    assert '"facets": _as_payload_json' in svc, "в payload фасет обязан быть объектом, а не строкой"


def test_ручная_правка_проверяет_закрытый_перечень():
    src = (ROOT / "src/api/routes/admin_models.py").read_text(encoding="utf-8")
    assert "фасеты вне закрытых перечней" in src, "значение вне перечня должно приводить к отказу"


def test_страница_фильтрует_по_предмету_защиты_мягко():
    page = (ROOT / "src/api/static/documents.html").read_text(encoding="utf-8")
    assert 'id="facet-filter"' in page
    assert "protection_subject" in page
    assert "действует только на список на странице" in page


def test_карточка_и_список_отдают_одни_и_те_же_поля():
    """Иначе проверка видит расхождение там, где его нет: у карточки фасетов не было вовсе."""
    src = (ROOT / "src/api/routes/upload.py").read_text(encoding="utf-8")
    start = src.index('"document_type": cfg_document_type')
    block = src[start:start + 600]
    assert '"rubrics":' in block and '"facets":' in block and '"collection":' in block, \
        "карточка документа обязана отдавать темы, фасеты и маршрут — как список"


def test_нет_жёсткого_фильтра_по_фасетам():
    """Фасет — такой же мягкий признак, как тема: в поиске по нему не отсекаем."""
    offenders = []
    for path in ROOT.joinpath("src").rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'key\s*=\s*["\'](facets|protection_subject)["\']', line):
                offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:90]}")
    assert not offenders, "жёсткий фильтр по фасетам запрещён:\n" + "\n".join(offenders)
