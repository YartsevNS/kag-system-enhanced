"""Связи между ДОКУМЕНТАМИ по идентификаторам — без модели, сопоставлением.

Зачем отдельный модуль. Связи «взамен», «во исполнение», «ссылается на» существуют между документами,
а не между фрагментами. Фрагментный поиск их не даёт в принципе: чтобы понять, что приказ ссылается на
ГОСТ, нужен документ целиком (или его шапка) и второй документ в базе. При этом угадывать моделью тут
не надо: документы сами называют друг друга — «вводится взамен ГОСТ Р 57580.1-2017», «во исполнение
приказа № 123», «в соответствии с 152-ФЗ».

Как это работает (детерминированно, без вызовов модели):
  1. у каждого документа берём ЕГО идентификаторы: номер и «семейство» (ГОСТ, СТО, приказ, ФЗ…);
  2. в тексте документа ищем УПОМИНАНИЯ других идентификаторов (переиспользуем правила
     `deterministic_extractors`, там же формы номеров и стандартов);
  3. сопоставляем упоминание с карточкой документа в базе по нормализованному ключу
     (семейство + номер) — это точное совпадение, а не догадка;
  4. по обороту речи выбираем тип связи: «взамен/заменяет» → replaces, «изменения/дополнения» → amends,
     «во исполнение/в соответствии» → derived-from, «приложение» → in-collection, иначе references;
  5. пишем связь через СЛУЖБУ ГРАФА (единый путь записи) со слоем `regex` и полным происхождением:
     версия схемы, версия извлекателя, фрагмент-источник.

Границы (важно не переоценить):
  * сопоставление работает только когда у документа ЕСТЬ идентификатор. Письма без номера так не
    свяжутся — для них остаётся извлечение моделью с пометкой «предположение»;
  * «отменяет» и «является приложением» часто подразумеваются, а не написаны: такие связи мы не
    выставляем фактом — только те, где оборот есть в тексте.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# «Семейство» идентификатора → как выглядит в тексте. Порядок важен: сначала более длинные формы.
_FAMILIES: Tuple[Tuple[str, str], ...] = (
    ("ГОСТ Р ИСО/МЭК", r"ГОСТ\s+Р\s+ИСО\s*/?\s*МЭК"),
    ("ГОСТ Р", r"ГОСТ\s+Р"),
    ("ГОСТ", r"ГОСТ"),
    ("ПНСТ", r"ПНСТ"),
    ("СТО", r"СТО"),
    ("СП", r"СП"),
    ("СНиП", r"СНиП"),
    ("СанПиН", r"СанПиН"),
    ("ФЗ", r"ФЗ"),
    ("ПРИКАЗ", r"приказ"),
    ("ПОСТАНОВЛЕНИЕ", r"постановлени"),
    ("РАСПОРЯЖЕНИЕ", r"распоряжени"),
    ("УКАЗАНИЕ", r"указани"),
    ("ПИСЬМО", r"письмо"),
    ("ПОЛОЖЕНИЕ", r"положение"),
    ("ИНСТРУКЦИЯ", r"инструкци"),
    ("РЕГЛАМЕНТ", r"регламент"),
    ("МЕТОДИКА", r"методик"),
)

# Номер после идентификатора: 57580.1-2017, 34.10-2012, 17, 123-П, 152-ФЗ
_NUMBER = re.compile(r"№?\s*(\d{1,6}(?:[.\-/]\d{1,6})*(?:\s*-\s*\d{2,4})?(?:\s*-\s*[А-ЯA-Z]{1,3})?)")

# Обороты речи, по которым виден тип отношения. Ищем в предложении рядом с упоминанием.
_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("replaces", r"взамен|в замену|заменяет|отменяет|вместо ранее"),
    ("amends", r"о внесении изменений|внесении изменений|изменения в|дополнени[яй] в|в редакции"),
    ("in-collection", r"приложени[еия]\s*№?\s*\d*\s*к|является приложением"),
    ("derived-from", r"во исполнение|на основании|в соответствии с|согласно"),
    ("references", r"ссылается на|упоминает|указанн\w+ в"),
)

FAMILY_TITLES: Dict[str, str] = dict(_FAMILIES)


@dataclass(frozen=True)
class Ident:
    """Идентификатор документа: семейство + нормализованный номер."""

    family: str
    number: str

    @property
    def key(self) -> str:
        return f"{self.family}|{self.number}"

    def __str__(self) -> str:
        return f"{self.family} {self.number}"


def _norm_number(raw: str) -> str:
    """Нормализация номера: убрать пробелы, свести тире, верхний регистр, убрать ведущий ноль у года."""
    s = (raw or "").strip().upper().replace("—", "-").replace("–", "-")
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s+", "", s)
    return s


def own_identifiers(title: str) -> List[Ident]:
    """Идентификаторы САМОГО документа — из его названия/шапки.

    Примеры: «ГОСТ Р 57580.1-2017» → (ГОСТ Р, 57580.1-2017); «Приказ ФСТЭК России № 17» →
    (ПРИКАЗ, 17); «Федеральный закон от … № 152-ФЗ» → (ФЗ, 152-ФЗ).
    """
    out: List[Ident] = []
    text = title or ""
    low = text.lower()
    for family, pattern in _FAMILIES:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        tail = text[m.end():m.end() + 40]
        num = _NUMBER.search(tail)
        if num:
            out.append(Ident(family, _norm_number(num.group(1))))
            break  # одного идентификатора в названии достаточно: он и определяет документ
    # Отдельный случай: «152-ФЗ» — номер стоит ПЕРЕД «ФЗ»
    if not out:
        m = re.search(r"(\d{2,4})\s*-\s*ФЗ", text, re.IGNORECASE)
        if m:
            out.append(Ident("ФЗ", _norm_number(m.group(1) + "-ФЗ")))
    # Фолбэк для названий без семейства: «№ 123», «123-П»
    if not out:
        m = re.search(r"№\s*(\d{1,6}(?:[-/][А-ЯA-Z0-9]{1,6})?)", text)
        if m:
            out.append(Ident("НОМЕР", _norm_number(m.group(1))))
    _ = low
    return out


def mentions(text: str, only_families: Optional[Iterable[str]] = None) -> List[Tuple[Ident, str]]:
    """Упоминания идентификаторов в тексте вместе с предложением, где они встретились.

    Предложение нужно, чтобы выбрать тип связи по обороту речи («взамен», «во исполнение»).
    """
    if not text:
        return []
    allow = set(only_families) if only_families else None
    found: List[Tuple[Ident, str]] = []
    seen: set = set()
    taken: List[Tuple[int, int]] = []      # принятые совпадения — против наложений
    for family, pattern in _FAMILIES:
        if allow is not None and family not in allow:
            continue
        # Короткие семейства (СТО, СП, ФЗ) рискуют совпасть внутри слова («наСТОящий», «иСПолнение») —
        # им проверяем границы. Длинным нельзя: русские окончания («приказа», «постановления») — норма,
        # и запрет на букву справа отсекал бы настоящие упоминания.
        _strict = len(family) <= 3
        for m in re.finditer(pattern, text, re.IGNORECASE):
            # Наложение: «ГОСТ» внутри уже принятого «ГОСТ Р» — тот же номер, дубль не нужен.
            if any(not (m.end() <= a or m.start() >= b) for a, b in taken):
                continue
            _lb = text[m.start() - 1] if m.start() > 0 else " "
            _rb = text[m.end()] if m.end() < len(text) else " "
            if _strict and (_lb.isalpha() or _rb.isalpha()):
                continue
            tail = text[m.end():m.end() + 40]
            num = _NUMBER.search(tail)
            ident: Optional[Ident] = None
            if num:
                ident = Ident(family, _norm_number(num.group(1)))
            elif family == "ФЗ":
                # Форма «152-ФЗ»: номер стоит ПЕРЕД семейством.
                _mb = re.search(r"(\d{2,4})\s*-\s*$", text[max(0, m.start() - 8):m.start()])
                if _mb:
                    ident = Ident("ФЗ", _norm_number(_mb.group(1) + "-ФЗ"))
            if ident is None or ident.key in seen:
                continue
            # Предложение с упоминанием: окно фиксированной длины захватывало соседний оборот
            # («взамен … во исполнение …»), и тип связи выбирался по чужому предложению.
            left = max(0, m.start() - 500)
            window = text[left:m.end() + 500].replace("\n", " ")
            offset = m.start() - left
            start = 0
            for pos in [0] + [x.end() for x in re.finditer(r"[.!?]\s", window)]:
                if pos <= offset:
                    start = pos
            ctx = window[start:]
            _dot = ctx.find(". ")
            if _dot != -1:
                ctx = ctx[:_dot + 1]
            seen.add(ident.key)
            taken.append((m.start(), m.end()))
            found.append((ident, ctx.strip()))
    return found


def relation_type_for(context: str) -> str:
    """Тип связи по обороту речи в предложении. Без оборота — `references` (ссылка по умолчанию)."""
    low = (context or "").lower()
    for code, pattern in _MARKERS:
        if re.search(pattern, low):
            return code
    return "references"


def build_index(documents: List[Dict]) -> Dict[str, str]:
    """Индекс «идентификатор → id документа» по карточкам документов (id + название).

    Ключ — нормализованный (семейство + номер). Если один идентификатор у нескольких документов
    (так бывает: старая и новая редакция), берём ПЕРВЫЙ по списку, а остальные видны в отчёте.
    """
    index: Dict[str, str] = {}
    for d in documents:
        doc_id = str(d.get("id") or d.get("document_id") or "")
        title = str(d.get("recognized_title") or d.get("filename") or "")
        if not doc_id or not title:
            continue
        for ident in own_identifiers(title):
            index.setdefault(ident.key, doc_id)
    return index


# ── Прогон: сопоставить документы и записать связи ─────────────────────────────

# Где искать упоминания: шапка документа и фрагменты с оборотом-ссылкой. Сканировать все
# фрагменты всех документов незачем — ссылки на другие документы живут в шапке и в оборотах.
_HEAD_CHUNKS = 3
_MARKER_SCAN = re.compile(
    r"взамен|во исполнение|в соответствии|на основании|о внесении изменений|в редакции|приложение",
    re.IGNORECASE,
)


async def link_documents(document_ids: Optional[List[str]] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    """Найти и записать связи между документами. Возвращает отчёт для журнала и проверки.

    Порядок: берём карточки документов → строим индекс идентификаторов → по каждому документу
    смотрим шапку и фрагменты с оборотами-ссылками → сопоставляем с индексом → пишем связь
    через службу графа со слоем `regex`.
    """
    from src.api.services.document_repository import get_doc_repo
    from src.indexing.embeddings_service import embeddings_service
    from src.indexing.knowledge_graph import Relation, kg_service

    repo = get_doc_repo()
    docs = repo.get_all(limit=None) if hasattr(repo, "get_all") else []
    if not docs:
        # Запасной путь: тот же источник, что у анализатора документов.
        from src.api.services.document_service import document_service

        docs = list(getattr(document_service, "_documents_cache", {}).values())
    docs = [d for d in docs if isinstance(d, dict)]
    if document_ids:
        want = {str(x) for x in document_ids}
        docs = [d for d in docs if str(d.get("id")) in want]
    if limit:
        docs = docs[: int(limit)]

    index = build_index(docs)
    by_id = {str(d.get("id")): d for d in docs}

    total_rels, per_doc, skipped = 0, 0, 0
    report: List[Dict[str, Any]] = []
    for d in docs:
        doc_id = str(d.get("id"))
        title = str(d.get("recognized_title") or d.get("filename") or "")
        own = own_identifiers(title)
        try:
            chunks = await embeddings_service.get_document_chunks(doc_id)
        except Exception:
            chunks = []
        if not chunks:
            skipped += 1
            continue

        rels: List[Any] = []
        for i, ch in enumerate(chunks):
            text = ch.get("content") or ""
            head = i < _HEAD_CHUNKS
            if not head and not _MARKER_SCAN.search(text):
                continue
            for ident, ctx in mentions(text):
                other_id = index.get(ident.key)
                if not other_id or other_id == doc_id:
                    continue
                rel_type = relation_type_for(ctx)
                rels.append(Relation(
                    source=doc_id, target=other_id, type=rel_type,
                    document_id=doc_id, chunk_id=ch.get("chunk_id") or "",
                    layer="regex",
                    source_label="Document", source_key="id",
                    target_label="Document", target_key="id",
                ))
        if rels:
            # Один документ может упоминать другой несколько раз — оставляем уникальные тройки.
            uniq = {}
            for r in rels:
                uniq[(r.source, r.target, r.type)] = r
            rels = list(uniq.values())
            try:
                kg_service.batch_create_relations(rels)
                total_rels += len(rels)
                per_doc += 1
                if len(report) < 25:
                    report.append({
                        "document": title[:60],
                        "own": [str(x) for x in own],
                        "links": len(rels),
                        "examples": [f"{r.type} → {str(by_id.get(r.target, {}).get('recognized_title') or r.target)[:40]}"
                                     for r in rels[:3]],
                    })
            except Exception as exc:  # noqa: BLE001
                report.append({"document": title[:60], "error": str(exc)[:120]})

    return {
        "documents": len(docs),
        "with_identifiers": len(index),
        "linked_documents": per_doc,
        "relations_written": total_rels,
        "skipped_no_chunks": skipped,
        "examples": report,
    }
