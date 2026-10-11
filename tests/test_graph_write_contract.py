"""Контракт записи в граф: поля, которые конвейер передаёт моделям, должны существовать в моделях.

Живой дефект 11.10.2026 (стоил потери всех связей документа и целого цикла выката): поле `evidence`
добавили в модель СУЩНОСТИ (`Entity`), где оно не используется, а конвейер передаёт его в модель
СВЯЗИ (`Relation`). Запись связей падала на КАЖДОМ фрагменте — «Relation.__init__() got an
unexpected keyword argument 'evidence'» — и в граф попадали одни сущности, без единой связи.
Юнит-тесты этого не видели: никто не сверял ВЫЗОВ в конвейере с ПОЛЯМИ модели.

Проверка статическая и мгновенная: читаем исходник конвейера, собираем ключевые аргументы вызовов
Entity(...) и Relation(...) и сверяем с полями dataclass'ов.
"""
import dataclasses
import re
from pathlib import Path

from src.indexing.knowledge_graph import Entity, Relation

ROOT = Path(__file__).resolve().parents[1]


def _kwargs_of(src: str, call: str) -> set[str]:
    """Ключевые аргументы во всех вызовах call(...) внутри исходника."""
    found: set[str] = set()
    for m in re.finditer(rf"\b{call}\(", src):
        # Собираем от открывающей скобки до закрывающей на её уровне.
        depth = 0
        body = []
        for ch in src[m.end() - 1:]:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            if depth >= 1:
                body.append(ch)
        found |= set(re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=", "".join(body)))
    return found


def test_relation_kwargs_exist_in_model():
    src = (ROOT / "src/indexing/entity_extractor.py").read_text(encoding="utf-8")
    fields = {f.name for f in dataclasses.fields(Relation)}
    unknown = _kwargs_of(src, "Relation") - fields
    assert not unknown, f"конвейер передаёт в Relation несуществующие поля: {sorted(unknown)}"


def test_entity_kwargs_exist_in_model():
    src = (ROOT / "src/indexing/entity_extractor.py").read_text(encoding="utf-8")
    fields = {f.name for f in dataclasses.fields(Entity)}
    unknown = _kwargs_of(src, "Entity") - fields
    assert not unknown, f"конвейер передаёт в Entity несуществующие поля: {sorted(unknown)}"


def test_relation_keeps_evidence_and_entity_does_not():
    """Цитата-основание принадлежит СВЯЗИ: доказательство лежит рядом с фактом."""
    rel = Relation(source="ГОСТ Р 57580.1—2017", target="Росстандарт", type="issued-by",
                   evidence="Росстандарт")
    assert rel.evidence == "Росстандарт"
    fields = {f.name for f in dataclasses.fields(Entity)}
    assert "evidence" not in fields, "evidence относится к связи, а не к сущности"


def test_citation_normalization_is_shared():
    """Проверка цитаты в конвейере и в схеме извлечения использует ОДНУ нормализацию.

    Класс дефекта: две разные нормы (схлопывание пробелов против «только буквы и цифры») дают
    разный ответ на распознанном тексте — тире, кавычки и переносы в цитате модели и в тексте
    фрагмента не совпадают буквально. При строгом режиме «писать только подтверждённое» это молча
    вырезало бы ВЕРНЫЕ связи.
    """
    from src.indexing.graph_ontology import flat_evidence_text

    assert flat_evidence_text("ГОСТ Р 57580.1—2017!") == "гост р 57580 1 2017"
    assert flat_evidence_text("взамен\nГОСТ  Р 34.12—2015") == "взамен гост р 34 12 2015"
    ext = (ROOT / "src/indexing/entity_extractor.py").read_text(encoding="utf-8")
    v2 = (ROOT / "src/indexing/extraction_v2.py").read_text(encoding="utf-8")
    assert "flat_evidence_text" in ext, "конвейер не берёт общую нормализацию цитаты"
    assert "flat_evidence_text" in v2, "схема извлечения не берёт общую нормализацию цитаты"
    assert 're.sub(r"\\s+", " "' not in ext, "в конвейере осталась СВОЯ нормализация цитаты"


def test_batch_write_keeps_evidence():
    """Пачечная запись обязана класть цитату: конвейер пишет связи ПАЧКОЙ, не поодиночке.

    Живой дефект 11.10.2026: цитату добавили только в одиночную запись (create_relation), а
    batch_create_relations её не передавал — после пересборки документа из 1765 связей ни у одной
    не было цитаты. Проверка смотрит сам запрос: параметр в батче и SET у связи.
    """
    src = (ROOT / "src/indexing/knowledge_graph.py").read_text(encoding="utf-8")
    i = src.index("def batch_create_relations(")
    i_end = src.index("def ", i + 10)
    block = src[i:i_end]
    assert '"evidence": getattr(r, "evidence", "") or ""' in block, \
        "батч не передаёт цитату в параметрах запроса"
    assert "rel.evidence = CASE WHEN" in block, \
        "батч не записывает цитату на связь (пустую дозаполняет, непустую не затирает)"


def test_single_write_keeps_evidence():
    src = (ROOT / "src/indexing/knowledge_graph.py").read_text(encoding="utf-8")
    i = src.index("def create_relation(")
    i_end = src.index("def ", i + 10)
    assert "rel.evidence = CASE WHEN" in src[i:i_end]
