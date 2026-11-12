"""Контекст употребления термина: несколько слов до и после, с самим термином.

Зачем: решение «это один и тот же термин или нет» нельзя принимать по двум строкам.
Человеку нужен живой фрагмент: где термин встретился, что стоит до и после. Берём
фрагменты поиском по самому термину и оставляем только те, где он реально есть
подстрокой (поиск семантический и может вернуть близкое по смыслу).
"""
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_WORD = re.compile(r"\S+")


def snippet(text: str, term: str, before: int = 12, after: int = 14) -> str:
    """Фрагмент вокруг первого вхождения термина: N слов до, термин, M слов после.

    Термин размечается звёздочками (**термин**) — страница сама превращает их в жирный.
    """
    body = " ".join((text or "").split())
    if not body or not term:
        return ""
    low_body, low_term = body.lower(), term.lower()
    pos = low_body.find(low_term)
    if pos < 0:
        return ""
    words = list(_WORD.finditer(body))
    # Какое по счёту слово — начало вхождения
    start_idx = next((i for i, m in enumerate(words) if m.start() <= pos < m.end()), None)
    if start_idx is None:
        start_idx = next((i for i, m in enumerate(words) if m.end() > pos), 0)
    end_idx = next((i for i, m in enumerate(words) if m.end() >= pos + len(low_term)), start_idx)
    left = max(0, start_idx - before)
    right = min(len(words), end_idx + 1 + after)
    parts = []
    for i in range(left, right):
        word = body[words[i].start():words[i].end()]
        if start_idx <= i <= end_idx:
            parts.append(f"**{word}**")
        else:
            parts.append(word)
    prefix = "… " if left > 0 else ""
    suffix = " …" if right < len(words) else ""
    return prefix + " ".join(parts) + suffix


async def find_context(term: str, limit: int = 5, scan: int = 40) -> List[Dict[str, Any]]:
    """Фрагменты с термином: документ, имя файла, размеченный сниппет."""
    needle = (term or "").strip()
    if len(needle) < 2:
        return []
    try:
        from src.indexing.embeddings_service import embeddings_service

        hits = await embeddings_service.search(query=needle, limit=scan, is_admin=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("контекст термина: поиск не выполнен: %s", str(exc)[:160])
        return []

    out: List[Dict[str, Any]] = []
    seen = set()
    for hit in hits or []:
        payload = hit.get("payload") or {}
        text = payload.get("content") or payload.get("text") or hit.get("content") or ""
        if needle.lower() not in text.lower():
            continue
        doc_id = payload.get("document_id") or hit.get("document_id") or ""
        chunk_id = payload.get("chunk_id") or hit.get("chunk_id") or ""
        key = (chunk_id or doc_id, text[:80])
        if key in seen:
            continue
        seen.add(key)
        marked = snippet(text, needle)
        if not marked:
            continue
        out.append({
            "document_id": doc_id,
            "filename": payload.get("filename") or "",
            "chunk_id": chunk_id,
            "text": marked,
            "score": round(float(hit.get("score") or 0.0), 4),
        })
        if len(out) >= limit:
            break
    return out
