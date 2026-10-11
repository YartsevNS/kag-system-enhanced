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


def test_model_write_uses_evidence():
    """Запись связи кладёт цитату в граф (иначе доказательство теряется, как было до правки)."""
    src = (ROOT / "src/indexing/knowledge_graph.py").read_text(encoding="utf-8")
    assert "rel.evidence = coalesce(rel.evidence, $evidence)" in src
