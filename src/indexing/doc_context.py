"""Единственный сборщик контекста документа: вид (код словаря) и document_key.

Почему отдельный модуль. Раньше контекст собирался в одном месте, но из неверного
источника (строка documents вместо source_metadata), и вид брался как есть — поэтому в
онтологию попадало слово модели «гост» вместо кода national_standard, а document_key
падал на название документа. Проверки журнала при этом писали «ожидаемое» тем же
сырым значением, и лог сам вводил в заблуждение.

Источник правды по видам — src/indexing/document_kinds.py (единый словарь, 29 кодов).
Здесь НЕТ своего списка кодов: только перевод человекочитаемых формулировок в код
и одна формула ключа. Второй список кодов разошёлся бы с словарём на первой правке.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from src.indexing import document_kinds

# Тире в номерах приходят разными символами (OCR, вставка из PDF): «—», «–», «−».
_DASHES = "\u2014\u2013\u2212\u2015\u2010\u2011"

# Человекочитаемое → код словаря. Нужно для старых метаданных и для внешних источников
# (web_monitor), которые присылают слово, а не код. Ключи нормализованы: нижний регистр,
# без пунктуации, одиночные пробелы.
_ALIASES: Dict[str, str] = {
    # стандартизация
    "гост": "national_standard",
    "гост р": "national_standard",
    "гост р исо": "national_standard",
    "гост исо": "national_standard",
    "национальный стандарт": "national_standard",
    "стандарт": "national_standard",
    "пнст": "preliminary_standard",
    "предварительный национальный стандарт": "preliminary_standard",
    "сто": "org_standard",
    "стандарт организации": "org_standard",
    "ту": "specification",
    "технические условия": "specification",
    "сп": "code_of_practice",
    "свод правил": "code_of_practice",
    "р": "standardization_recommendation",
    "рекомендации": "standardization_recommendation",
    "рекомендации по стандартизации": "standardization_recommendation",
    # нормативные и правовые
    "закон": "law",
    "кодекс": "law",
    "фз": "law",
    "федеральный закон": "law",
    "приказ": "subordinate_act",
    "постановление": "subordinate_act",
    "распоряжение": "subordinate_act",
    "указ": "subordinate_act",
    "подзаконный документ": "subordinate_act",
    "устав": "charter",
    "решение": "decision",
    "протокол": "minutes",
    "положение": "regulation",
    "регламент": "regulation",
    "правила": "regulation",
    "инструкция": "instruction",
    "указание": "directive",
    "методика": "methodology",
    "методические рекомендации": "methodology",
    "методический документ": "methodology",
    "письмо": "official_letter",
    "информационное письмо": "official_letter",
    # хозяйственные
    "договор": "contract",
    "контракт": "contract",
    "соглашение": "contract",
    "дополнительное соглашение": "contract_amendment",
    "счет": "invoice",
    "счёт": "invoice",
    "счет-фактура": "invoice",
    "документ": "act",
    "накладная": "waybill",
    "доверенность": "power_of_attorney",
    "бланк": "form_template",
    "шаблон": "form_template",
    # информационные
    "отчет": "report",
    "отчёт": "report",
    "доклад": "analytics",
    "аналитика": "analytics",
    "публикация": "publication",
    "справка": "reference",
    "иное": "other",
    "прочее": "other",
}


def normalize_number(value: str) -> str:
    """Номер без украшений: тире одного вида, без пробелов вокруг дефиса и в концах."""
    s = str(value or "").strip()
    for d in _DASHES:
        s = s.replace(d, "-")
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s+", " ", s)
    # «№ 239», «N 239» — служебные знаки в ключ не идут
    s = re.sub(r"^(?:№|n|no\.?)\s*", "", s, flags=re.IGNORECASE)
    # В поле «номер» часто вписывают обозначение целиком: «ГОСТ Р 52447—2005».
    # Обозначение вида уходит в вид, в ключе остаётся номер: 52447-2005.
    s = re.sub(r"^(?:гост(?:\s+р)?|пнст|сто|ту|сп|р)\s+(?=\d)", "", s, flags=re.IGNORECASE)
    return s.strip(" ,.;")


def _norm_word(value: str) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"[«»\"'`]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .,:;")


def resolve_doc_type(*values: Optional[str]) -> Optional[str]:
    """Вид документа как код словаря. None — определить не удалось.

    Принимает несколько кандидатов в порядке приоритета (метаданные, строка documents,
    название). Сначала проверяет, не код ли это уже; потом ищет человекочитаемую
    формулировку — целиком, затем по первым словам («ГОСТ Р 52447—2005» → «гост р»).
    """
    candidates = [v for v in values if isinstance(v, str) and v.strip()]
    for raw in candidates:
        val = raw.strip()
        if document_kinds.is_valid(val):
            return val
    for raw in candidates:
        word = _norm_word(raw)
        if not word:
            continue
        if word in _ALIASES:
            return _ALIASES[word]
        # «гост р 52447—2005» → «гост р»; «приказ фстэк 239» → «приказ»
        parts = word.split()
        for n in (3, 2, 1):
            prefix = " ".join(parts[:n])
            if prefix in _ALIASES:
                return _ALIASES[prefix]
        # обозначение перед номером: «р 1323565.1.061—2024»
        first = re.split(r"[\s\d]", word, maxsplit=1)[0]
        if first and first in _ALIASES:
            return _ALIASES[first]
    return None


def _key_title(title: str) -> str:
    """Название для ключа: без скобочных уточнений и без вариаций дат.

    Зачем: у документов без номера ключ строится из названия. Модель пишет дату
    утверждения по-разному — «(утв. 11.02.2014)» и «(утв. 11 февраля 2014 г.)», —
    и один документ ложится в базу двумя записями regulatory_document
    (живой случай 13.11.2026 на «Меры защиты информации в ГИС»).
    Скобочные уточнения в ключ не входят: они не делают документ другим.
    """
    text = re.sub(r"\([^)]*\)", " ", title or "")
    text = re.sub(r"\s+", " ", text).strip(" .,;:-")
    return text


def build_document_key(issuer: str, doc_type_code: str, number: str, title: str = "") -> str:
    """Формула ключа документа из шапки схемы: «издатель|вид|номер».

    Вид — КОД словаря, а не слово: doc_type в онтологии хранится кодом, и ключ должен
    быть из того же пространства значений, иначе при первом новом doc_kind ключ и
    атрибут разойдутся. Номера нет — берётся название без скобочных уточнений
    (см. _key_title): иначе разное написание даты утверждения даёт два «разных» документа.
    """
    iss = _norm_word(issuer)
    num = normalize_number(number)
    if not num and title:
        num = _norm_word(_key_title(title) or title)
    parts = [p for p in (iss, doc_type_code or "", num) if p]
    return "|".join(parts)[:300]


def build_context(
    source_metadata: Optional[Dict[str, Any]],
    row: Optional[Dict[str, Any]] = None,
    title: str = "",
    document_id: str = "",
) -> Dict[str, str]:
    """Собрать контекст документа для извлечения и записи.

    Приоритет источников: source_metadata (то, что человек указал при загрузке) →
    строка documents → название. Контекст — единственный производитель значений;
    writer (typedb_writer._inject_doc_context) только читает его.
    """
    sm = source_metadata if isinstance(source_metadata, dict) else {}
    rw = row if isinstance(row, dict) else {}

    issuer = str(sm.get("issuer") or rw.get("issuer") or "").strip()
    number = str(sm.get("number") or rw.get("number") or "").strip()
    doc_type = resolve_doc_type(
        str(sm.get("doc_kind") or ""), str(rw.get("document_type") or ""),
        str(sm.get("doc_type") or ""), title or str(rw.get("recognized_title") or ""),
    ) or ""

    key = str(sm.get("document_key") or rw.get("document_key") or "").strip()
    if key and "(" in key:
        # Ключ, сохранённый при загрузке, мог содержать скобочное «(утв. 11.02.2014)»:
        # разное написание даты давало две записи одного документа. Чистим при чтении,
        # чтобы старые данные не тянули ошибку дальше.
        key = _key_title(key)
    if not key:
        # Есть номер — канонический ключ «издатель|вид|номер». Номера нет — ключ по файлу:
        # иначе в ключ попадает название, и один документ расходится с онтологией
        # (проверка key_mismatch дала 17 расхождений на 7 документах, 13.11.2026).
        if normalize_number(number) or not document_id:
            key = build_document_key(issuer, doc_type, number, title)
        else:
            key = f"kag:{document_id}"

    return {
        "document_key": key,
        "doc_type": doc_type,
        "issuer": issuer,
        "number": number or normalize_number(str(sm.get("number") or "")),
        "title": title or str(rw.get("recognized_title") or ""),
        "version_string": str(sm.get("version_string") or "") or "1",
        "version_status": str(sm.get("version_status") or "current"),
        "effective_from": str(sm.get("effective_from") or ""),
        "kag_document_id": document_id,
    }
