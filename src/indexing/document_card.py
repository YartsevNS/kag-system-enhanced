"""Карточка документа: контекст для эмбеддинга чанков и точка документа в Qdrant.

Зачем (пункт 2 плана под 2000 документов).

1. **Контекстуальный префикс.** Чанк «5.2.1 Требования к средствам защиты» без контекста
   почти неотличим от такого же пункта другого документа. Anthropic Contextual Retrieval
   решает это LLM-вызовом на КАЖДЫЙ чанк (−35…−49% неудачных поисков), но у нас документ
   уже анализируется одним вызовом (`document_analyzer` → title/type/summary/topics),
   поэтому префикс собирается из готовой карточки — **дополнительных LLM-вызовов нет**.
   Префикс добавляется только к документу (passage), запрос эмбеддится как есть.

2. **Точка документа (`level=document`).** В Qdrant кладём отдельную точку с карточкой —
   она нужна для «поиска по документам» и как основа сравнений (вопросы вида «сравни
   требования в A и B» решаются по карточкам, а не по 2000 чанкам). Поиск чанков эту точку
   обязан исключать (`must_not level=document`), иначе карточка попадёт в выдачу чата
   как «фрагмент документа».

Ничего не пишет в БД: карточка = производное от полей записи документа
(`recognized_title`, `document_type`, `summary`, `topics`), источник правды — запись.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Префикс идёт в КАЖДЫЙ чанк документа: длинный размывает сигнал самого чанка,
# поэтому держим его коротким (~30–40 токенов, как в Contextual Retrieval).
CARD_PREFIX_MAX_CHARS = 200
# Сколько текста документа показываем анализатору для карточки: начало + образцы.
CARD_SOURCE_MAX_CHARS = 4000
# Уровень точки документа в payload (у чанков level отсутствует).
LEVEL_DOCUMENT = "document"

_CARD_NAMESPACE = "kag-doc-card:"


def card_point_id(document_id: str) -> str:
    """Детерминированный id точки-карточки в Qdrant."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{_CARD_NAMESPACE}{document_id}"))


def build_card_source(chunks: List[Dict[str, Any]], max_chars: int = CARD_SOURCE_MAX_CHARS) -> str:
    """Текст для анализа документа: начало + образцы середины и конца.

    Почему не только первый чанк: у ГОСТа первый чанк — титул и шапка, по ним карточка
    выходит «Рекомендации по стандартизации», хотя документ про конкретную тему.
    Образцы середины дают тему, при этом объём ограничен (один LLM-вызов).
    """
    texts = [(c.get("content") or "").strip() for c in (chunks or [])]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0][:max_chars]

    # начало берём щедро (титул + оглавление), остальное — по одному образцу
    head_budget = max_chars // 2
    head = texts[0][:head_budget]
    rest = texts[1:]
    # 1–2 образца из середины/конца, поровну
    sample_idx = sorted({len(rest) // 2, len(rest) - 1})
    per_sample = max(1, (max_chars - len(head)) // max(1, len(sample_idx)))
    parts = [head]
    for i in sample_idx:
        if 0 <= i < len(rest):
            parts.append(f"[фрагмент {i + 2}] " + rest[i][:per_sample])
    return "\n".join(parts)[:max_chars]


def card_from_record(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Карточка документа из полей записи (без обращения к LLM)."""
    doc = doc or {}
    topics = doc.get("topics")
    if isinstance(topics, str):
        try:
            import json

            topics = json.loads(topics)
        except Exception:
            topics = []
    if not isinstance(topics, list):
        topics = []
    return {
        "document_id": str(doc.get("id") or ""),
        "filename": str(doc.get("filename") or ""),
        "title": str(doc.get("recognized_title") or "").strip(),
        "document_type": str(doc.get("document_type") or "").strip(),
        "summary": str(doc.get("summary") or "").strip(),
        "topics": [str(t).strip() for t in topics if str(t).strip()][:5],
    }


def build_card_prefix(card: Dict[str, Any], max_chars: int = CARD_PREFIX_MAX_CHARS) -> str:
    """Короткий префикс для эмбеддинга чанка.

    Порядок значимости: название (что за документ) → тип → темы (о чём он).
    Пустые поля не добавляются; если карточки нет — возвращаем пустую строку
    (текст для эмбеддинга остаётся прежним, fail-open).
    """
    title = (card.get("title") or "").strip()
    dtype = (card.get("document_type") or "").strip()
    topics = [t for t in (card.get("topics") or []) if t][:3]
    summary = (card.get("summary") or "").strip()

    parts: List[str] = []
    if title:
        parts.append(title)
    if dtype and dtype.lower() not in ("other", "unknown", "неизвестно"):
        parts.append(dtype)
    if topics:
        parts.append("темы: " + ", ".join(topics))
    elif summary:
        parts.append(summary)

    prefix = ". ".join(parts).strip()
    if not prefix:
        return ""
    if len(prefix) > max_chars:
        prefix = prefix[: max_chars - 1].rstrip() + "…"
    return prefix


def build_card_text(card: Dict[str, Any]) -> str:
    """Текст карточки для отдельной точки (level=document) в Qdrant."""
    parts: List[str] = []
    if card.get("title"):
        parts.append(card["title"])
    if card.get("document_type"):
        parts.append(f"Тип документа: {card['document_type']}")
    if card.get("summary"):
        parts.append(card["summary"])
    if card.get("topics"):
        parts.append("Темы: " + ", ".join(card["topics"]))
    if card.get("filename"):
        parts.append(f"Файл: {card['filename']}")
    return "\n".join(parts)


def acl_payload(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Поля прав доступа для точки-карточки — те же имена, что у чанков.

    Без них карточка документа была бы видна всем, хотя сам документ закрыт.
    """
    doc = doc or {}
    out: Dict[str, Any] = {"visibility": doc.get("visibility") or "public"}
    for key in ("allow_group_ids", "deny_group_ids", "allow_user_ids", "deny_user_ids"):
        val = doc.get(key)
        if isinstance(val, str):
            import json

            try:
                val = json.loads(val)
            except Exception:
                val = []
        out[key] = val if isinstance(val, list) else []
    return out


async def upsert_card_point(document_id: str, doc: Optional[Dict[str, Any]]) -> bool:
    """Положить карточку документа в Qdrant как точку level=document.

    Возвращает True, если точка записана. Ошибки глушим наружу (fail-open):
    карточка — улучшение поиска, её отсутствие не должно ломать обработку.
    """
    card = card_from_record(doc)
    text = build_card_text(card)
    if not text.strip():
        return False

    from src.indexing.embeddings_service import embeddings_service

    await embeddings_service.initialize()
    await embeddings_service.ensure_model()

    client = embeddings_service._qdrant_client
    if client is None:
        return False

    vectors = await embeddings_service._embedding_client.generate_batch([text])
    if not vectors:
        return False

    payload: Dict[str, Any] = {
        "document_id": document_id,
        "chunk_id": "card",
        "level": LEVEL_DOCUMENT,
        "content": text,
        "filename": card.get("filename") or "",
        "title": card.get("title") or "",
        "document_type": card.get("document_type") or "",
        "summary": card.get("summary") or "",
        "topics": card.get("topics") or [],
    }
    payload.update(acl_payload(doc))

    from qdrant_client import models as qm

    point = qm.PointStruct(
        id=card_point_id(document_id),
        vector={"dense": list(vectors[0])},
        payload=payload,
    )
    await _to_thread(client.upsert, collection_name=embeddings_service.collection_name, points=[point])
    logger.info(f"[card] точка документа обновлена: {document_id[:8]} ({len(text)} симв.)")
    return True


# ── вспомогательное ───────────────────────────────────────────────────────────

async def _to_thread(fn, *args, **kwargs):
    """Синхронный клиент Qdrant — только в потоке (иначе блокируем event loop)."""
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)
