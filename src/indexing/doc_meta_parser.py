"""Разбор шапки документа при загрузке: вид, номер, издатель, дата, название.

Зачем: эти пять полей человек вводил руками, и именно поэтому в онтологию попадало
«гост» вместо кода national_standard — ручной ввод обходил словарь видов. Теперь форму
заполняет парсер по самому файлу, а ручной ввод остаётся только как «уточнить» после
ошибки (запись extraction_failed в /errors).

Парсер намеренно простой и предсказуемый: заголовок первых страниц, регулярные шаблоны,
перевод человекочитаемого в код теми же функциями, что использует конвейер
(src/indexing/doc_context.py, src/indexing/issuers.py). Если не распознал — возвращает
None и список missing, а не «угадывает» other: решение принимает вызывающий код.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict, List, Optional

from src.indexing import doc_context, issuers

logger = logging.getLogger(__name__)

# Обозначения документов: ГОСТ Р 52447—2005, ГОСТ 34.601-90, Р 1323565.1.061-2024,
# ПНСТ, СТО, СП, ТУ. Номер берём вместе с обозначением, чистим normalize_number.
_DESIGNATION = re.compile(
    r"\b(ГОСТ(?:\s+Р)?(?:\s+ИСО)?|ПНСТ|СТО|СП|ТУ|Р)\s*[№N]?\s*"
    # номер без хвостового тире; год через тире берём отдельной группой: «1323565.1.061— 2024»
    r"(\d(?:[\d.\-—–‐‑]*\d)?)(?:\s*[—–]\s*(\d{4}))?",
    re.IGNORECASE,
)
_ORDER = re.compile(
    r"\b(Приказ|Постановление|Распоряжение|Указание)\b[^\n]{0,60}?№\s*([\d\-/]+)",
    re.IGNORECASE,
)
_LAW = re.compile(r"\b(\d{2,3})\s*-\s*ФЗ\b")
_EFFECTIVE = re.compile(
    r"(?:вступает\s+в\s+силу|введен[аы]?\s+в\s+действие|вводится\s+в\s+действие|"
    r"дата\s+введения)\s*[—:\-–]?\s*(?:с\s+)?"
    # день-месяц-год: 01.02.2025
    r"(\d{1,2})[.\s—–\-](\d{1,2})[.\s—–\-](\d{4})",
    re.IGNORECASE,
)
# год-месяц-день: «Дата введения — 2025—01—01» (частый вид в рекомендациях)
_EFFECTIVE_YMD = re.compile(
    r"(?:вступает\s+в\s+силу|введен[аы]?\s+в\s+действие|вводится\s+в\s+действие|"
    r"дата\s+введения)\s*[—:\-–]?\s*"
    r"(\d{4})[—–.\-](\d{1,2})[—–.\-](\d{1,2})",
    re.IGNORECASE,
)
_EFFECTIVE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _text_head(content: bytes, filename: str = "", pages: int = 8) -> str:
    """Первые страницы текста или начало файла. Пустая строка — прочитать не удалось."""
    name = (filename or "").lower()
    try:
        if name.endswith(".pdf") or content[:4] == b"%PDF":
            import fitz  # pymupdf

            with fitz.open(stream=content, filetype="pdf") as doc:
                out = []
                for i, page in enumerate(doc):
                    if i >= pages:
                        break
                    out.append(page.get_text() or "")
                return "\n".join(out)
        if name.endswith((".docx", ".doc")):
            import docx  # python-docx

            d = docx.Document(io.BytesIO(content))
            return "\n".join(p.text for p in d.paragraphs[:60] if p.text)
        if name.endswith((".html", ".htm")):
            raw = content.decode("utf-8", "replace")
            raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
            text = re.sub(r"(?s)<[^>]+>", " ", raw)
            return re.sub(r"\s+", " ", text)
        return content[:20000].decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — парсер не должен валить загрузку
        logger.warning("парсер шапки: текст не прочитан (%s): %s", filename, str(exc)[:160])
        return ""


def _iso(value: str) -> Optional[str]:
    m = _EFFECTIVE_ISO.search(value or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def parse_document_meta(
    filename: str,
    content: bytes,
    source_url: str = "",
    source_kind: str = "",
) -> Dict[str, Any]:
    """Вернуть source_metadata, собранный по файлу. Что не нашлось — в missing.

    Поля:
      number        — номер/обозначение документа («52447-2005», «1323565.1.061-2024», «239»)
      doc_kind      — КОД вида из словаря (national_standard, subordinate_act, …)
      issuer        — издатель человекочитаемо (Росстандарт, ФСТЭК России, …)
      effective_from— дата вступления в силу, ISO
      title         — название документа
      missing       — список полей, которые определить не удалось
    """
    # Шапка (вид, номер, издатель, название) — с первых ДВУХ страниц: этого достаточно,
    # и не тянем весь документ. Дата вступления в силу часто стоит дальше (в рекомендациях
    # встречается на 5-й странице), поэтому её ищем отдельным проходом по первым восьми.
    head = _text_head(content, filename, pages=2)
    tail = _text_head(content, filename, pages=8)
    first = "\n".join(l.strip() for l in head.splitlines() if l.strip())[:4000]
    out: Dict[str, Any] = {"missing": []}

    # 1. Номер и вид — по обозначению в шапке
    kind_raw = ""
    number = ""
    m = _DESIGNATION.search(first)
    if m:
        kind_raw = m.group(1)
        num = doc_context.normalize_number(m.group(2))
        year = m.group(3)
        if num:
            number = f"{num}-{year}" if year else num
            out["_designation"] = number
    if not number:
        m = _ORDER.search(first)
        if m:
            kind_raw, number = m.group(1), doc_context.normalize_number(m.group(2))
    if not number:
        m = _LAW.search(first)
        if m:
            kind_raw, number = "ФЗ", f"{m.group(1)}-ФЗ"

    code = doc_context.resolve_doc_type(kind_raw, first[:200]) if kind_raw else None
    if code:
        out["doc_kind"] = code
    else:
        out["missing"].append("doc_kind")
    if number:
        out["number"] = number
    else:
        out["missing"].append("number")

    # 2. Издатель — по URL источника, затем по шапке
    issuer = ""
    url = (source_url or "").lower()
    if "fstec.ru" in url:
        issuer = "ФСТЭК России"
    elif "rst.gov.ru" in url or "protect.gost.ru" in url:
        issuer = "Росстандарт"
    elif "cbr.ru" in url:
        issuer = "Банк России"
    elif "digital.gov.ru" in url:
        issuer = "Минцифры России"
    if not issuer:
        # Ищем орган прямо в шапке по всем его названиям (полное, краткое, историческое):
        # «Федеральное агентство по техническому регулированию и метрологии» = Росстандарт.
        found = issuers.detect_in_text(first)
        if found:
            issuer = found[1]
    if issuer:
        out["issuer"] = issuer
    elif (source_kind or "").lower() == "regulator":
        out["missing"].append("issuer")

    # 3. Дата вступления в силу
    mm = _EFFECTIVE_YMD.search(tail)
    if mm:
        y, mth, d = mm.groups()
        out["effective_from"] = f"{y}-{int(mth):02d}-{int(d):02d}"
    else:
        mm = _EFFECTIVE.search(tail)
        if mm:
            a, b, y = mm.groups()
            # порядок бывает разный: «01.02.2025» (день-месяц) — приводим к ISO
            d, mth = (a, b) if int(a) <= 31 and int(b) <= 31 else (b, a)
            out["effective_from"] = f"{y}-{int(mth):02d}-{int(d):02d}"
        else:
            iso = _iso(tail)
            if iso:
                out["effective_from"] = iso
            else:
                out["missing"].append("effective_from")

    # 4. Название — первая содержательная строка после обозначения
    title = ""
    for line in (l.strip() for l in head.splitlines()):
        if len(line) < 12 or len(line) > 300:
            continue
        if re.fullmatch(r"[\d\s.,\-—–]+", line):
            continue
        low = line.lower()
        if low.startswith(("утвержден", "утверждена", "введен", "издание официальное")):
            continue
        if re.search(r"федеральное агентство|федеральная служба|министерство", low):
            continue
        title = line
        break
    if title:
        out["title"] = title
    return out
