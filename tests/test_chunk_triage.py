"""Триаж чанков: штампы и дубли не идут в LLM-извлечение.

Зачем: извлечение стоит 2 LLM-вызова на чанк (~3 с), а часть чанков служебная
(колонтитулы, копирайты, повторы). Замер на корпусе: самые похожие между документами
чанки — ровно такие блоки (sim=1.000 у «Уведомление и тексты размещаются…»).
Модуль ничего не пишет: классифицирует чанки, решение — за конвейером.
"""
from pathlib import Path

import pytest

from src.indexing import chunk_triage
from src.indexing.chunk_triage import TriageResult, _pairwise_similarity, _same_text

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "src/api/services/document_service.py").read_text(encoding="utf-8")


def _chunks(texts):
    return [{"chunk_id": f"c{i}", "content": t, "metadata": {"chunk_seq": i + 1}}
            for i, t in enumerate(texts)]


def test_same_text_ignores_case_and_spaces():
    assert _same_text("Уведомление и тексты ", "уведомление и тексты")
    assert not _same_text("одно", "другое")


def test_pairwise_similarity_identical_vectors():
    """Идентичные вектора дают косинус 1.0; нулевые пары не возвращаются.

    Нулевое сходство отбрасывается намеренно: для триажа нужны только пары выше
    порога (0.95+), а хранение всех пар — лишняя память на больших документах.
    """
    pairs = _pairwise_similarity([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    sims = {(i, j): round(s, 3) for i, j, s in pairs}
    assert sims[(0, 1)] == 1.0
    assert (0, 2) not in sims, "пары с нулевым сходством не нужны триажу"


def test_summary_counts_and_skip():
    r = TriageResult(kept=[0], boilerplate=[1], duplicate=[2])
    s = r.summary()
    assert s["kept"] == 1 and s["boilerplate"] == 1 and s["duplicate"] == 1
    assert s["skipped"] == 2
    assert r.skip == {1, 2}


@pytest.mark.asyncio
async def test_duplicate_inside_document_is_skipped():
    """Второй экземпляр того же текста внутри документа — дубль."""
    chunks = _chunks(["Правила контроля доступа к информации.",
                      "Правила контроля доступа к информации.",
                      "Иной текст про криптографию."])
    vectors = {0: [1.0, 0.0, 0.0], 1: [1.0, 0.0, 0.0], 2: [0.0, 1.0, 0.0]}

    async def fake_fetch(document_id, chs, embed_service):
        return vectors

    async def fake_neighbors(vector, limit):
        return []          # чужих совпадений нет

    original = chunk_triage._fetch_document_vectors
    chunk_triage._fetch_document_vectors = fake_fetch
    try:
        res = await chunk_triage.triage_chunks("doc-1", chunks, embed_service=object(),
                                              neighbors=fake_neighbors)
    finally:
        chunk_triage._fetch_document_vectors = original

    assert res.duplicate == [1], res.summary()
    assert 0 in res.kept and 2 in res.kept
    assert res.boilerplate == []


@pytest.mark.asyncio
async def test_boilerplate_when_seen_in_other_documents():
    """Тот же чанк ещё в двух чужих документах → штамп (извлечение не нужно)."""
    chunks = _chunks(["© Стандартинформ, 2018. Настоящие рекомендации не могут быть полностью воспроизведены."])
    vectors = {0: [1.0, 0.0]}

    async def fake_fetch(document_id, chs, embed_service):
        return vectors

    async def fake_neighbors(vector, limit):
        return [(0.999, "doc-2"), (0.998, "doc-3"), (0.65, "doc-4")]

    original = chunk_triage._fetch_document_vectors
    chunk_triage._fetch_document_vectors = fake_fetch
    try:
        res = await chunk_triage.triage_chunks("doc-1", chunks, embed_service=object(),
                                              neighbors=fake_neighbors)
    finally:
        chunk_triage._fetch_document_vectors = original

    assert res.boilerplate == [0], res.summary()
    assert res.kept == []


@pytest.mark.asyncio
async def test_similar_only_in_one_foreign_document_is_kept():
    """Один чужой документ — это ещё не штамп (может быть просто близкий текст)."""
    chunks = _chunks(["Требования к паролям: не менее 8 символов."])
    vectors = {0: [1.0, 0.0]}

    async def fake_fetch(document_id, chs, embed_service):
        return vectors

    async def fake_neighbors(vector, limit):
        return [(0.99, "doc-2")]

    original = chunk_triage._fetch_document_vectors
    chunk_triage._fetch_document_vectors = fake_fetch
    try:
        res = await chunk_triage.triage_chunks("doc-1", chunks, embed_service=object(),
                                              neighbors=fake_neighbors)
    finally:
        chunk_triage._fetch_document_vectors = original

    assert res.boilerplate == []
    assert res.kept == [0]


@pytest.mark.asyncio
async def test_failure_is_fail_open():
    """Сбой триажа не должен лишать документ графа: все чанки остаются в работе."""
    original = chunk_triage._fetch_document_vectors

    async def boom(document_id, chs, embed_service):
        raise RuntimeError("Qdrant недоступен")

    chunk_triage._fetch_document_vectors = boom
    try:
        res = await chunk_triage.triage_chunks("doc-1", _chunks(["текст"]), embed_service=object())
    finally:
        chunk_triage._fetch_document_vectors = original

    assert res.error
    assert res.kept == [0] and res.skip == set()


def test_pipeline_skips_llm_for_triaged_chunks():
    """Структурная проверка: конвейер читает триаж ДО извлечения и пропускает чанки."""
    assert "triage_chunks(" in SRC
    assert "graph_triage" in SRC
    i_triage = SRC.index("triage_chunks(")
    i_skip = SRC.index("if i in _skip_llm:")
    i_extract = SRC.index("entity_extractor.extract_and_store(")
    assert i_triage < i_skip < i_extract
