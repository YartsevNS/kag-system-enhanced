"""Качество текстового слоя PDF: распознать подмену шрифта и дать текст через OCR.

Зачем: у части PDF текстовый слой не пустой, но это не текст документа, а подстановка
глифов — «ФСТЭК РОССИИ» читается как «$ȿȾȿ ȺɅ0ɇȺ3». Проверки на пустоту и на кодировку
(cp1251/latin-1) такое не ловят: символов много, и они «похожи на буквы». Такой слой
уходил в векторы и в граф, документ становился ненаходимым, а шапка парсилась мусором
(живой случай: Приказ ФСТЭК России № 239, 11.11.2026).

Решение: посчитать долю символов вне обычных алфавитов и сверить язык слоя с языком
имени файла. Если слой подозрительный — отдать страницу распознавателю и взять его текст.
"""
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CYR = re.compile(r"[а-яё]", re.I)
LETTER = re.compile(r"[^\W\d_]", re.U)

# Символы, которые нормальный русский/английский текст содержит: буквы обоих алфавитов,
# цифры, обычная и типографская пунктуация. Всё прочее — признак подстановки глифов.
_ALLOWED = set(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    ".,;:!?()[]{}\"'«»„“”№-–—/−+*%=_|@#&~^<>/\\ \n\t\r"
)


def cyrillic_share(text: str) -> float:
    """Доля кириллицы среди букв текста (0..1)."""
    letters = [c for c in text if LETTER.match(c)]
    if not letters:
        return 0.0
    return sum(1 for c in letters if CYR.match(c)) / len(letters)


def garbage_share(text: str) -> float:
    """Доля символов вне обычных алфавитов и пунктуации (0..1)."""
    chars = [c.lower() for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if c not in _ALLOWED) / len(chars)


MIXED_TOKEN = re.compile(r"^(?=[^\s]*[A-Za-zЀ-ӿ])(?=[^\s]*\d)[A-Za-zЀ-ӿ\d]{4,}$")


def mixed_token_share(text: str) -> float:
    """Доля «слов», где буквы и цифры слиты: B59EC9, GF69D89A.

    В обычном русском тексте таких токенов почти нет (номер стоит отдельно:
    «ГОСТ Р 34.10-2012»), а при подмене глифов они идут сплошняком — это признак,
    не зависящий от языка имени файла.
    """
    tokens = [t for t in re.split(r"\s+", text) if t.strip()]
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if MIXED_TOKEN.match(t)) / len(tokens)


def looks_broken(text: str, name_hint: str = "") -> tuple:
    """Похож ли слой на подстановку глифов. Возвращает (подозрительно, причина)."""
    body = (text or "").strip()
    if len(body) < 200:
        return False, ""
    share = garbage_share(body)
    if share > 0.03:
        return True, f"мусорных символов {share:.1%}"
    mixed = mixed_token_share(body)
    if mixed > 0.03:
        return True, f"слитных буквенно-цифровых слов {mixed:.0%}"
    # Язык слоя против языка имени файла: у русского документа слой без кириллицы —
    # это подмена шрифта, а не английский текст (имя файла русское).
    if CYR.search(name_hint or "") and len(LETTER.findall(body)) >= 50:
        cyr = cyrillic_share(body)
        if cyr < 0.10:
            return True, f"кириллицы в слое {cyr:.0%} при русском имени файла"
    return False, ""


def ocr_page_text(png: bytes) -> Optional[str]:
    """Текст страницы от распознавателя: служба → локальный движок → None."""
    try:
        from src.indexing.ocr_client import (
            lines_from_service,
            lines_local,
            service_available,
            service_enabled,
        )

        lines: List[Dict] = []
        if service_enabled() and service_available():
            lines = lines_from_service(png)
        if not lines:
            lines = lines_local(png)
    except Exception as exc:  # noqa: BLE001 — сбой движка не должен ломать разбор
        logger.warning(f"OCR страницы не выполнен: {type(exc).__name__}: {str(exc)[:120]}")
        return None
    if not lines:
        return None
    try:
        from src.indexing.reading_order import order_page

        ordered = order_page(lines)
        if ordered.text.strip():
            return ordered.text
    except Exception as exc:  # noqa: BLE001 — без геометрии берём порядок движка
        logger.debug(f"порядок чтения не применён: {exc}")
    return "\n".join(str(line.get("text") or "") for line in lines).strip() or None


def ocr_pdf_pages(path, dpi: int = 200, max_pages: Optional[int] = None) -> Dict[int, str]:
    """Распознать страницы PDF: {номер страницы (с нуля): текст}."""
    import pymupdf

    out: Dict[int, str] = {}
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"OCR PDF {Path(str(path)).name}: файл не открыт: {exc}")
        return out
    try:
        limit = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
        for i in range(limit):
            pix = doc[i].get_pixmap(dpi=dpi)
            text = ocr_page_text(pix.tobytes("png"))
            if text:
                out[i] = text
    finally:
        doc.close()
    return out
