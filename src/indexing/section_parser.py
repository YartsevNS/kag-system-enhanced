"""Разбор структуры документа на разделы: узлы Section + хлебная крошка.

Зачем.

Структура в документе есть, но не размечена: заголовки лежат в тексте, а в графе у чанка нет
ни номера раздела, ни хлебной крошки. Следствия: префикс эмбеддинга говорит только «что за
документ», но не «какой раздел»; сравнивать разделы A и B нельзя; саммари приходится делать на
чанк, а не на раздел (на 2000 документах это разница в числе LLM-вызовов).

Что делает.

Заголовки распознаются по тексту: «4.3 Требования к монтажу», «Раздел 4», «Приложение А»,
строка ЗАГЛАВНЫМИ. Каждый чанк относится к последнему заголовку не позже себя, крошка —
«<документ> / 4.3 Требования к монтажу».

Ограничения (сознательные):
* ищем заголовок только в первых строках чанка — иначе разрез попадёт в середину текста;
* если заголовков больше 40% чанков — считаем это текстом, а не структурой, и не размечаем;
* самообозначение документа (колонтитул) заголовком не считается — переиспользуем
  `entity_selfref`, чтобы крошка не начиналась с номера самого документа.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.indexing.entity_selfref import is_self_reference, self_reference_keys

# «4.3 Требования к монтажу», «5.2.1 Общие положения»
_NUMBERED = re.compile(r"^\s{0,4}(\d{1,2}(?:\.\d{1,3}){0,3})[.)]?\s+([А-ЯЁA-Z][^\n]{2,90})$")
# «Раздел 4», «Глава III», «Приложение А»
_CHAPTER = re.compile(
    r"^\s{0,4}((?:Раздел|Глава|Приложение)\s+[А-ЯЁA-Z0-9IVXLC]{1,6})\s*[—\-–:.]?\s*(.{0,80})$",
    re.IGNORECASE,
)
# Строка ЗАГЛАВНЫМИ: «ТРЕБОВАНИЯ К МОНТАЖУ»
_UPPER = re.compile(r"^\s{0,4}([А-ЯЁ][А-ЯЁ0-9\s\-–—,.:«»()]{6,70})\s*$")

# Служебные строки, которые не являются заголовком раздела
_NOISE = re.compile(
    r"(стандартинформ|все права|воспроизведен|уведомление|©|https?://|\.pdf|"
    r"^\d{4}-\d{2}-\d{2}$|^\d+$)",
    re.IGNORECASE,
)

# Страховка от «все чанки — заголовки»: больше 40% чанков с заголовками — подозрительно
_MAX_HEADING_SHARE = 0.4
_MAX_SECTIONS = 300


# Строка, которая является обозначением/ссылкой, а не заголовком раздела:
# «Р 1323565.1.004—2017», «ГОСТ Р 34.10-2012», «1323565.1.048—2023». Признак — почти нет слов.
_DESIGNATION_LINE = re.compile(
    r"^\s*(?:ГОСТ(?:\s+Р)?|Р|СТО|ИСО|ISO|МЭК|IEC)?\s*[\d.\-–—\s]{4,}$",
    re.IGNORECASE,
)


def _looks_like_designation_line(line: str) -> bool:
    if not _DESIGNATION_LINE.match(line):
        return False
    letters = sum(1 for ch in line if ch.isalpha())
    return letters <= 3  # «Р 1323565.1.004—2017» — буква одна, это не заголовок


def _first_lines(text: str, count: int = 2) -> List[str]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[:count]


def detect_heading(text: str, filename: str = "") -> Optional[Dict[str, str]]:
    """Заголовок в начале чанка → {"number", "title"} или None."""
    keys, cores = self_reference_keys(filename) if filename else (set(), set())
    for line in _first_lines(text):
        if len(line) < 4 or _NOISE.search(line):
            continue
        if keys and is_self_reference(line, keys, cores):
            continue  # колонтитул/самообозначение, а не заголовок
        if _looks_like_designation_line(line):
            continue  # обозначение документа/ссылка на стандарт, а не заголовок раздела
        m = _NUMBERED.match(line)
        if m:
            return {"number": m.group(1), "title": m.group(2).strip()}
        m = _CHAPTER.match(line)
        if m:
            return {"number": m.group(1).strip(), "title": (m.group(2) or "").strip()}
        m = _UPPER.match(line)
        if m:
            title = m.group(1).strip()
            # Заголовок может быть одним словом («ВВЕДЕНИЕ», «ОГЛАВЛЕНИЕ», «ТЕРМИНЫ»),
            # поэтому требуем не число слов, а осмысленную длину слова и не слишком
            # длинную строку (иначе в заголовки попадёт абзац заглавными).
            words = title.split()
            if 1 <= len(words) <= 12 and len(title) >= 4:
                return {"number": "", "title": title}
    return None


def parse_sections(chunks: List[Dict[str, Any]], filename: str = "") -> List[Dict[str, Any]]:
    """Разметить чанки по разделам (в порядке появления).

    Возвращает список: [{"section_index", "number", "title", "chunk_indexes", "chunk_ids", "section_id"}]
    """
    if not chunks:
        return []

    headings: List[Dict[str, Any]] = []
    for i, ch in enumerate(chunks):
        head = detect_heading(str(ch.get("content") or ""), filename)
        if head:
            headings.append({"chunk_index": i, **head})

    if not headings:
        return []
    if len(headings) > _MAX_SECTIONS:
        return []
    # Похоже, «заголовками» оказался обычный текст: если их доля больше 40% при документе
    # от 10 чанков — размечать не будем (иначе каждый абзац станет «разделом»).
    if len(chunks) >= 10 and len(headings) > int(len(chunks) * _MAX_HEADING_SHARE):
        return []

    sections: List[Dict[str, Any]] = []
    for pos, head in enumerate(headings):
        start = head["chunk_index"]
        end = headings[pos + 1]["chunk_index"] - 1 if pos + 1 < len(headings) else len(chunks) - 1
        idxs = list(range(start, end + 1))
        if not idxs:
            continue
        sections.append({
            "section_index": len(sections),
            "number": head.get("number") or "",
            "title": head.get("title") or "",
            "chunk_indexes": idxs,
            "chunk_ids": [str((chunks[i] or {}).get("chunk_id") or "") for i in idxs],
        })
    return sections


def build_breadcrumb(doc_label: str, number: str = "", title: str = "",
                     max_chars: int = 90) -> str:
    """Хлебная крошка: «<документ> / 4.3 Название»."""
    parts: List[str] = []
    if (doc_label or "").strip():
        parts.append(doc_label.strip())
    section = " ".join([p for p in [number, title] if p]).strip()
    if section:
        parts.append(section)
    crumb = " / ".join(parts)
    if len(crumb) > max_chars:
        crumb = crumb[: max_chars - 1].rstrip() + "…"
    return crumb


def sections_for_chunks(chunks: List[Dict[str, Any]], doc_label: str,
                        filename: str = "") -> Dict[int, str]:
    """Крошка для каждого чанка: {индекс чанка: крошка}. Пусто — структуры не нашли."""
    sections = parse_sections(chunks, filename)
    if not sections:
        return {}
    out: Dict[int, str] = {}
    for sec in sections:
        crumb = build_breadcrumb(doc_label, sec["number"], sec["title"])
        for i in sec["chunk_indexes"]:
            out[i] = crumb
    return out
