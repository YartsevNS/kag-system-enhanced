"""Сравнительные вопросы: структурированный контекст по сторонам сравнения.

Зачем. На вопрос «чем отличаются требования TLS 1.2 и TLS 1.3» поиск отдаёт фрагменты из разных
документов вперемешку, и модель отвечает «по мотивам», без привязки к тому, что относится к какому
документу. При этом у нас уже есть готовые кирпичи: карточки документов (level=document),
хлебная крошка разделов и фильтр поиска по document_id.

Что делает модуль:
* определяет, что вопрос сравнительный (по маркерам: сравни/отлича/разница/против/vs);
* собирает единый блок контекста: на каждую сторону — карточка документа (название, тип, тема)
  и её фрагменты с указанием раздела, а не «солянку» из чанков;
* даёт инструкцию к ответу: сначала общее, потом отличия по пунктам с указанием документа и раздела.

Выключение: `config_store` namespace `chat`, ключ `comparison` = {"enabled": bool}
(по умолчанию включено; флаг нужен для честного A/B — включено/выключено на одном наборе вопросов).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Маркеры сравнения. «или» само по себе слишком частое, поэтому пара признаков: маркер
# сравнения ИЛИ два и более союза перечисления в вопросе.
_COMPARISON_MARKERS = ("сравн", "отлича", "разниц", "различ", "чем … от", " против ", " vs ", "vs.")
_ENUM_MARKERS = (" и ", " а также ", ", ", " или ")

COMPARISON_INSTRUCTION = (
    "ВОПРОС СРАВНИТЕЛЬНЫЙ. Отвечай двумя блоками: сначала «Общее» (что совпадает), "
    "затем «Отличия» — по пунктам. В каждом пункте указывай, к какому документу и разделу он "
    "относится. Если по одному из сравниваемых объектов данных в контексте нет — скажи прямо, "
    "не додумывай."
)


def comparison_enabled() -> bool:
    """Включён ли сравнительный режим (config_store: chat/comparison, по умолчанию да)."""
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("chat", "comparison") or {}
        if isinstance(cfg, dict) and "enabled" in cfg:
            return bool(cfg["enabled"])
    except Exception:
        pass
    return True


def is_comparison_question(query: str) -> bool:
    """Похож ли вопрос на сравнительный (по маркерам, без LLM)."""
    q = (query or "").strip().lower()
    if len(q) < 25:
        return False
    if any(m in q for m in _COMPARISON_MARKERS):
        return True
    # «какие требования в A и B», «что сказано про X и Y» — перечисление без слова «сравнить»
    enum_count = sum(1 for m in _ENUM_MARKERS if m in q)
    return enum_count >= 2 and (" и " in q or " или " in q)


def build_comparison_context(sides: List[Dict[str, Any]], chunks_per_side: int = 4,
                             max_chunk_chars: int = 900) -> str:
    """Собрать блок контекста для сравнения.

    sides: [{"query", "card": {title, document_type, summary, topics, filename},
             "chunks": [{"content"/"text", "breadcrumb"}]}]
    """
    if not sides:
        return ""
    parts: List[str] = []
    for i, side in enumerate(sides, 1):
        card = side.get("card") or {}
        head = (card.get("title") or card.get("filename") or side.get("query") or "").strip()
        lines = [f"=== Сравниваемый объект {i}: {head} ==="]
        if card.get("document_type"):
            lines.append(f"тип документа: {card['document_type']}")
        if card.get("summary"):
            lines.append(f"о чём: {str(card['summary'])[:300]}")
        topics = [str(t) for t in (card.get("topics") or [])][:4]
        if topics:
            lines.append("темы: " + ", ".join(topics))
        chunks = (side.get("chunks") or [])[:chunks_per_side]
        if not chunks:
            lines.append("(фрагменты по этому объекту в базе не найдены)")
        for ch in chunks:
            text = str(ch.get("content") or ch.get("text") or "").strip()[:max_chunk_chars]
            crumb = str(ch.get("breadcrumb") or "").strip()
            lines.append(f"— {crumb + ': ' if crumb else ''}{text}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# Вводные слова, которые не являются стороной сравнения
_LEAD_RE = r"^\s*(?:сравни(?:ть)?|сравнение|чем\s+отлича(?:ются|ется)|в\s+чём\s+разниц(?:а|ы)|разница\s+между|что\s+общего)\s*"
_TAIL_RE = r"^\s*(?:требовани(?:я|й|е)\s+(?:к|в)|правила|порядок)\s*"
_SPLIT_RE = r"\s+(?:и|против|vs\.?|—|–)\s+"


def split_comparison_sides(query: str) -> List[str]:
    """Разбить сравнительный вопрос на две стороны БЕЗ LLM: «сравнить X и Y» → [X, Y].

    Почему не через _decompose_query: у него предварительное условие «вопрос длиннее 60
    символов», поэтому короткие сравнительные вопросы («сравнить ГОСТ Р 34.11 и 34.13») он
    не разбирал, и режим сравнения МОЛЧА не включался ни разу (проверка лога: 0 срабатываний
    при включённом флаге). Разделение по союзу работает без модели и мгновенно.
    """
    import re as _re
    q = _re.sub(_LEAD_RE, "", (query or "").strip(), flags=_re.IGNORECASE)
    q = _re.sub(_TAIL_RE, "", q, flags=_re.IGNORECASE)
    parts = _re.split(_SPLIT_RE, q, flags=_re.IGNORECASE)
    cleaned: List[str] = []
    for part in parts:
        part = _re.sub(r"^(?:и|а\s+также)\s+", "", part.strip(" .,?;:"), flags=_re.IGNORECASE)
        if len(part) >= 3:
            cleaned.append(part.strip())
    return cleaned[:2] if len(cleaned) >= 2 else []


def comparison_entities(query: str, subqueries: List[str]) -> Optional[List[str]]:
    """Стороны сравнения: подвопросы, если их два и больше; иначе None."""
    subs = [s.strip() for s in (subqueries or []) if s and s.strip()]
    if len(subs) >= 2:
        return subs[:2]
    return None
