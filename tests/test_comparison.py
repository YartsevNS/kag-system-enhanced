"""Сравнительный режим: определение вопроса и сборка контекста по сторонам."""
from pathlib import Path

from src.indexing.comparison import (
    COMPARISON_INSTRUCTION,
    build_comparison_context,
    comparison_enabled,
    comparison_entities,
    is_comparison_question,
)


class FakeStore:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, ns, key, default=None):
        return self.data.get(f"{ns}:{key}", default)


# ── определение сравнительного вопроса ───────────────────────────────────────

def test_сравнительные_вопросы_определяются():
    for q in ("чем отличаются требования TLS 1.2 и TLS 1.3 с российскими криптоалгоритмами",
              "сравнить ГОСТ Р 34.11-2012 и ГОСТ Р 34.13-2015",
              "в чём разница между выработкой общего ключа и транспортным контейнером"):
        assert is_comparison_question(q), q


def test_обычные_вопросы_не_сравнительные():
    for q in ("какие требования к хэш-функции Стрибог",
              "где хранить ключи электронной подписи",
              "какая погода в Москве завтра"):
        assert not is_comparison_question(q), q


def test_короткий_вопрос_не_сравнительный():
    assert not is_comparison_question("сравни")


# ── стороны и контекст ───────────────────────────────────────────────────────

def test_стороны_из_подвопросов():
    assert comparison_entities("x", ["требования TLS 1.2", "требования TLS 1.3"]) == \
        ["требования TLS 1.2", "требования TLS 1.3"]
    assert comparison_entities("x", ["один подвопрос"]) is None
    assert comparison_entities("x", []) is None


def test_контекст_содержит_оба_документа():
    sides = [
        {"query": "TLS 1.2", "card": {"document_id": "d1", "title": "Р 1323565.1.020—2020",
                                      "document_type": "standard", "topics": ["TLS"]},
         "chunks": [{"content": "TLS 1.2 использует российские криптонаборы", "breadcrumb": "Р 1323565.1.020—2020 / 5 Требования"}]},
        {"query": "TLS 1.3", "card": {"document_id": "d2", "title": "Р 1323565.1.030—2020",
                                      "document_type": "standard", "topics": ["TLS 1.3"]},
         "chunks": [{"content": "TLS 1.3 использует Кузнечик и Магму", "breadcrumb": "Р 1323565.1.030—2020 / 4 Область"}]},
    ]
    ctx = build_comparison_context(sides)
    assert "Сравниваемый объект 1" in ctx and "Сравниваемый объект 2" in ctx
    assert "Р 1323565.1.020—2020" in ctx and "Р 1323565.1.030—2020" in ctx
    assert "TLS 1.2 использует" in ctx and "Кузнечик" in ctx
    assert "Р 1323565.1.020—2020 / 5 Требования" in ctx, "крошка раздела должна попадать в контекст"


def test_контекст_без_чанков_не_врёт():
    ctx = build_comparison_context([{"query": "A", "card": {"title": "Док А"}, "chunks": []}])
    assert "фрагменты по этому объекту в базе не найдены" in ctx
    assert build_comparison_context([]) == ""


def test_инструкция_требует_двух_блоков():
    assert "Общее" in COMPARISON_INSTRUCTION and "Отличия" in COMPARISON_INSTRUCTION


# ── выключатель ──────────────────────────────────────────────────────────────

def test_флаг_по_умолчанию_включён(monkeypatch):
    import importlib
    cs_mod = importlib.import_module("src.api.services.config_store")
    monkeypatch.setattr(cs_mod, "config_store", FakeStore({}), raising=False)
    assert comparison_enabled() is True


def test_флаг_можно_выключить(monkeypatch):
    import importlib
    cs_mod = importlib.import_module("src.api.services.config_store")
    monkeypatch.setattr(cs_mod, "config_store",
                        FakeStore({"chat:comparison": {"enabled": False}}), raising=False)
    assert comparison_enabled() is False


# ── проводка в чат ───────────────────────────────────────────────────────────

def test_сравнительный_режим_подключён():
    src = Path("src/api/services/chat_service.py").read_text(encoding="utf-8")
    assert "async def _comparison_context" in src
    assert "COMPARISON_INSTRUCTION" in src
    assert "search_documents" in src, "стороны берутся по карточкам документов"
    assert 'filters={"document_id": doc_id}' in src, "фрагменты — в границах своего документа"


def test_разбиение_сторон_без_llm():
    """Короткие сравнительные вопросы: режим обязан включаться (был случай 0 срабатываний)."""
    from src.indexing.comparison import split_comparison_sides
    assert split_comparison_sides("сравнить ГОСТ Р 34.11-2012 и ГОСТ Р 34.13-2015") == \
        ["ГОСТ Р 34.11-2012", "ГОСТ Р 34.13-2015"]
    got = split_comparison_sides("чем отличаются требования TLS 1.2 и TLS 1.3 с российскими криптоалгоритмами")
    # «требования» остаётся в стороне намеренно: для поиска «требования TLS 1.2» точнее, чем
    # просто «TLS 1.2» — важно, что стороны разделены и вторая не потеряла уточнение.
    assert len(got) == 2 and got[0].endswith("TLS 1.2"), got
    assert "TLS 1.3" in got[1], got
    got2 = split_comparison_sides("сравнить требования к выработке общего ключа и транспортным ключевым контейнерам")
    assert len(got2) == 2 and "выработке" in got2[0], got2
    assert split_comparison_sides("какие требования к хэш-функции Стрибог") == []
