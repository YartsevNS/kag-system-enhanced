"""Триаж чанков перед LLM-извлечением: штампы и дубли не стоят LLM-вызовов.

Зачем. Извлечение сущностей — два LLM-вызова на каждый чанк (~3 с). При этом заметная
часть чанков служебная: колонтитулы, копирайты, «уведомление размещается также…»,
оглавления, повторы внутри документа. Замер на нашем корпусе (3749 чанков) показал, что
самые похожие между документами чанки — ровно такие блоки (sim=1.000 у «Уведомление и
тексты размещаются также в информационной системе общего пользования…»).

Что делает:
* **внутридокументный дубль** — чанк почти совпадает с другим чанком того же документа
  (sim >= CHUNK_DUP_SIM): извлечение делаем один раз, у первого;
* **штамп** — такой же чанк встречается ещё минимум в CHUNK_BOILERPLATE_MIN_DOCS-1 чужих
  документах (sim >= CHUNK_BOILERPLATE_SIM): извлечение не нужно вообще.

Модуль ничего не пишет в базы: возвращает классификацию по индексам чанков, решение
принимает конвейер. Вектора документа берутся одним обращением к Qdrant (они уже
посчитаны на шаге эмбеддинга), меж-документные соседи — ANN-запросом по тому же вектору.

Стоимость: одно обращение scroll на документ + по одному ANN-запросу на чанк
(75 чанков × ~10 мс ≈ 0.75 с) — против ~3 с LLM на каждый отсеянный чанк.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

try:  # numpy есть в образе; если нет — работаем чистым Python (медленнее, но верно)
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

# Сколько ANN-запросов держим одновременно (Qdrant локальный, но не стоит заваливать)
_SEARCH_CONCURRENCY = 4
_EXAMPLES_LIMIT = 5


@dataclass
class TriageResult:
    """Классификация чанков документа: индексы в исходном списке chunks."""

    kept: List[int] = field(default_factory=list)
    boilerplate: List[int] = field(default_factory=list)
    duplicate: List[int] = field(default_factory=list)
    examples: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str = ""

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "kept": len(self.kept),
            "boilerplate": len(self.boilerplate),
            "duplicate": len(self.duplicate),
            "skipped": len(self.boilerplate) + len(self.duplicate),
            "elapsed_ms": round(self.elapsed_ms, 1),
        }
        if self.examples:
            out["examples"] = self.examples
        if self.error:
            out["error"] = self.error
        return out

    @property
    def skip(self) -> set[int]:
        """Индексы чанков, для которых LLM-извлечение не нужно."""
        return set(self.boilerplate) | set(self.duplicate)


def _pairwise_similarity(vecs: List[List[float]]) -> List[tuple[int, int, float]]:
    """Пары (i, j, косинус) для i < j. numpy, если доступен."""
    n = len(vecs)
    pairs: List[tuple[int, int, float]] = []
    if n < 2:
        return pairs
    if _np is not None:
        m = _np.asarray(vecs, dtype=_np.float32)
        norms = _np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normed = m / norms
        sim = normed @ normed.T
        for i in range(n):
            for j in range(i + 1, n):
                value = float(sim[i, j])
                if value > 0:
                    pairs.append((i, j, value))
        return pairs
    import math

    for i in range(n):
        for j in range(i + 1, n):
            a, b = vecs[i], vecs[j]
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            if na and nb:
                pairs.append((i, j, dot / (na * nb)))
    return pairs


def _same_text(a: str, b: str) -> bool:
    """Дешёвая проверка «одно и то же» для коротких служебных строк."""
    return a.strip().lower() == b.strip().lower()


async def _fetch_document_vectors(document_id: str, chunks: List[Dict[str, Any]],
                                  embed_service) -> Dict[int, List[float]]:
    """Вектора чанков документа из Qdrant: {индекс чанка: вектор}.

    Сопоставляем по payload.chunk_id (он есть у всех точек независимо от схемы id) —
    это надёжнее, чем вычислять point_id: в корпусе есть чанки, записанные по старой
    схеме uuid5(document_id-i), и такой расчёт не находит их (живой случай: получено
    0 векторов из 558, триаж молча отнёс всё в «оставить»). Резервный путь —
    point_id = uuid5(chunk_id) для точек без payload.chunk_id.
    """
    from src.indexing.ids import point_id_for_chunk

    by_chunk_id: Dict[str, List[float]] = {}
    by_point_id: Dict[str, List[float]] = {}

    def _scroll():
        flt = {"must": [{"key": "document_id", "match": {"value": document_id}}]}
        offset = None
        while True:
            points, offset = embed_service._qdrant_client.scroll(
                collection_name=embed_service.collection_name,
                scroll_filter=flt, limit=500, with_payload=True,
                with_vectors=True, offset=offset,
            )
            for p in points:
                vec = p.vector
                if isinstance(vec, dict):
                    vec = vec.get("dense")
                if not vec:
                    continue
                by_point_id[str(p.id)] = list(vec)
                cid = (p.payload or {}).get("chunk_id")
                if cid:
                    by_chunk_id[str(cid)] = list(vec)
            if offset is None:
                break

    await asyncio.to_thread(_scroll)

    vectors: Dict[int, List[float]] = {}
    for i, ch in enumerate(chunks):
        cid = ch.get("chunk_id")
        vec = by_chunk_id.get(str(cid)) if cid else None
        if vec is None and cid:
            vec = by_point_id.get(str(point_id_for_chunk(cid)))
        if vec is None and ch.get("id"):
            vec = by_point_id.get(str(ch["id"]))
        if vec:
            vectors[i] = vec
    return vectors

async def triage_chunks(document_id: str, chunks: List[Dict[str, Any]], *,
                        embed_service=None, neighbors=None,
                        boilerplate_sim: Optional[float] = None,
                        boilerplate_min_docs: Optional[int] = None,
                        dup_sim: Optional[float] = None) -> TriageResult:
    """Классифицировать чанки перед LLM-извлечением.

    neighbors — подменяемая функция для тестов:
        async def neighbors(vector, limit) -> [(score, document_id), ...]
    По умолчанию — ANN-запрос в Qdrant.
    """
    import time

    from src.config import get_settings

    started = time.perf_counter()
    st = get_settings()
    boilerplate_sim = st.CHUNK_BOILERPLATE_SIM if boilerplate_sim is None else boilerplate_sim
    boilerplate_min_docs = st.CHUNK_BOILERPLATE_MIN_DOCS if boilerplate_min_docs is None else boilerplate_min_docs
    dup_sim = st.CHUNK_DUP_SIM if dup_sim is None else dup_sim
    limit = st.CHUNK_TRIAGE_NEIGHBORS

    result = TriageResult()
    if not chunks:
        return result

    try:
        if embed_service is None:
            from src.indexing.embeddings_service import embeddings_service as embed_service  # noqa: F811

        vectors = await _fetch_document_vectors(document_id, chunks, embed_service)

        # Если вектора не сопоставились, триаж бессилен — это надо видеть в журнале,
        # а не принимать за «штампов нет» (живой случай: 0 из 558 из-за старой схемы id).
        if len(vectors) < len(chunks):
            missing = len(chunks) - len(vectors)
            result.error = f"vectors_missing: {missing} из {len(chunks)}"
            logger.warning(
                f"[triage] для {document_id} не найдено векторов: {missing} из {len(chunks)} "
                f"— триаж работает по остатку"
            )

        # ── 1. Внутридокументные дубли (локально, numpy) ────────────────────
        dup_indexes: set[int] = set()
        index_list = sorted(vectors.keys())
        local_vecs = [vectors[i] for i in index_list]
        for pos_i, pos_j, sim in _pairwise_similarity(local_vecs):
            i, j = index_list[pos_i], index_list[pos_j]
            if sim < dup_sim:
                continue
            text_i = str(chunks[i].get("content") or "")
            text_j = str(chunks[j].get("content") or "")
            # Слияние только при реальном совпадении текста (короткие служебные строки
            # иначе склеиваются по «похожести» и теряют смысл).
            if sim >= 0.999 or _same_text(text_i, text_j):
                dup_indexes.add(j)
                if len(result.examples) < _EXAMPLES_LIMIT:
                    result.examples.append({
                        "kind": "duplicate", "index": j, "sim": round(sim, 4),
                        "text": text_j[:90].replace("\n", " "),
                    })

        # ── 2. Штампы: тот же чанк в чужих документах (ANN) ─────────────────
        if neighbors is None:
            async def neighbors(vector, limit):  # type: ignore[misc]
                def _query():
                    resp = embed_service._qdrant_client.query_points(
                        collection_name=embed_service.collection_name,
                        query=vector, limit=limit,
                        with_payload=["document_id"], with_vectors=False,
                    )
                    out = []
                    for p in getattr(resp, "points", resp):
                        pl = p.payload or {}
                        out.append((float(p.score), str(pl.get("document_id") or "")))
                    return out
                return await asyncio.to_thread(_query)

        sem = asyncio.Semaphore(_SEARCH_CONCURRENCY)

        async def _check(i: int) -> bool:
            async with sem:
                try:
                    hits = await neighbors(vectors[i], limit)
                except Exception as e:  # сеть/коллекция — не ломаем обработку
                    logger.debug(f"[triage] ANN для чанка {i} не удался: {e}")
                    return False
            foreign_docs = {doc for score, doc in hits
                            if doc and doc != document_id and score >= boilerplate_sim}
            return len(foreign_docs) >= max(1, boilerplate_min_docs - 1)

        candidates = [i for i in index_list if i not in dup_indexes]
        flags = await asyncio.gather(*[_check(i) for i in candidates])
        boilerplate_indexes = {i for i, is_bp in zip(candidates, flags) if is_bp}
        for i in sorted(boilerplate_indexes):
            if len(result.examples) < _EXAMPLES_LIMIT + 3:
                result.examples.append({
                    "kind": "boilerplate", "index": i,
                    "text": str(chunks[i].get("content") or "")[:90].replace("\n", " "),
                })

        result.boilerplate = sorted(boilerplate_indexes)
        result.duplicate = sorted(dup_indexes)
        result.kept = [i for i in range(len(chunks)) if i not in result.skip]
    except Exception as e:  # триаж не должен ронять обработку
        result.error = f"{type(e).__name__}: {e}"
        logger.warning(f"[triage] триаж не выполнен для {document_id}: {e}")
        result.kept = list(range(len(chunks)))
        result.boilerplate, result.duplicate = [], []
    finally:
        result.elapsed_ms = (time.perf_counter() - started) * 1000
    return result
