"""Самообозначения документа: варианты из имени файла и фильтр сущностей."""
from pathlib import Path

from src.indexing.entity_selfref import (
    filter_self_references,
    is_self_reference,
    self_reference_keys,
)


def test_варианты_из_имени_файла_с_типом():
    keys, _ = self_reference_keys("24d001b4-0ac1-40fc-9bc8-01ca91786285_gost-r-34-2012.pdf")
    assert "gost-r-34-2012" in keys
    assert "34-2012" in keys, "вариант без ведущего типа документа"


def test_вариант_без_года():
    keys, _ = self_reference_keys("24d001b4-0ac1-40fc-9bc8-01ca91786285_r-1323565.1-2017.pdf")
    assert "1323565.1-2017" in keys and "1323565.1" in keys


def test_ядро_из_имени_без_разделителя():
    """«gost57580» — обозначение без дефиса: ядро выделяется из цифровой части."""
    keys, cores = self_reference_keys("223b6ed3-8f51-4932-94cf-d2a1c9b8e4f0_gost57580.pdf")
    assert "gost57580" in keys
    assert "57580" in cores
    assert is_self_reference("57580-2017", keys, cores)


def test_ссылка_на_соседний_документ_серии_остаётся():
    """Ключевая проверка: серия не должна схлопываться."""
    keys, cores = self_reference_keys("24d001b4-0ac1-40fc-9bc8-01ca91786285_r-1323565.1.pdf")
    assert is_self_reference("1323565.1", keys, cores)
    assert not is_self_reference("1323565.1.020—2020", keys, cores), \
        "ссылка на другой документ серии должна остаться в графе"
    assert not is_self_reference("1323565.1.048—2023", keys, cores)


def test_год_как_вариант_ядра():
    keys, cores = self_reference_keys("24d001b4-0ac1-40fc-9bc8-01ca91786285_gost-r-34.10.pdf")
    assert is_self_reference("34.10", keys, cores)
    assert is_self_reference("34.10-2012", keys, cores)


def test_смысловые_сущности_не_трогаем():
    keys, cores = self_reference_keys("24d001b4-0ac1-40fc-9bc8-01ca91786285_r-1323565.1.pdf")
    for name in ("криптографическая защита информации", "ГОСТ Р 34.11-2012",
                 "эллиптическая кривая", "Стандартинформ"):
        assert not is_self_reference(name, keys, cores), name


def test_фильтр_убирает_связи_с_отброшенным():
    ents = [{"name": "34—2012"}, {"name": "хэширование"}]
    rels = [{"source": "34—2012", "target": "хэширование", "type": "описывает"},
            {"source": "хэширование", "target": "ГОСТ Р 34.11-2012", "type": "упоминает"}]
    kept_e, kept_r, dropped = filter_self_references(ents, rels, "24d001b4-0ac1-40fc-9bc8-01ca91786285_gost-r-34-2012.pdf")
    assert dropped == ["34—2012"]
    assert [e["name"] for e in kept_e] == ["хэширование"]
    assert len(kept_r) == 1 and kept_r[0]["target"] == "ГОСТ Р 34.11-2012"


def test_обычное_имя_файла_ничего_не_ломает():
    ents = [{"name": "Положение о закупках"}, {"name": "ООО Ромашка"}]
    kept, _, dropped = filter_self_references(ents, [], "Квитанции.pdf")
    assert dropped == [] and len(kept) == 2


# ── частотный отсев в графе и страховка очереди ──────────────────────────────

def test_граф_умеет_отсев_самообозначений():
    src = Path("src/indexing/knowledge_graph.py").read_text(encoding="utf-8")
    assert "def drop_ubiquitous_reference_entities" in src
    assert "min_ratio" in src and "MENTIONS" in src
    body = src[src.index("def drop_ubiquitous_reference_entities"):]
    assert "DELETE m" in body, "удаляются только связи MENTIONS этого документа"


def test_отсев_подключён_в_конвейере_и_в_перестроении():
    ds = Path("src/api/services/document_service.py").read_text(encoding="utf-8")
    assert "kg_service.drop_ubiquitous_reference_entities" in ds
    tasks = Path("src/indexing/tasks.py").read_text(encoding="utf-8")
    assert "drop_ubiquitous_reference_entities" in tasks, \
        "перестроение графа тоже должно чистить самообозначения"


def test_галочка_графа_ставит_построение_в_очередь():
    """Пропуск графа не должен терять граф: документ уходит в maintenance-очередь."""
    src = Path("src/api/services/document_service.py").read_text(encoding="utf-8")
    i = src.index("graph_skipped")
    tail = src[i:i + 2000]
    assert "rebuild_graph_task" in tail and "maintenance" in tail
