"""Наполнение словаря терминов кандидатами из корпуса (ждёт решения человека).

Что извлекаем из фрагментов документов:
1. Пары «полное название (АББРЕВИАТУРА)» — самый ценный вид синонима:
   «средство защиты информации (СЗИ)».
2. Определения по ГОСТ: строка вида «3.1 автоматизированная система: Система, …» —
   термин слева от двоеточия (пара термин → тот же термин, как запись в глоссарии).

Ничего не подтверждается автоматически: все записи создаются с reviewed=false,
verdict='' — их решения принимает человек на странице /terms. Повторный прогон
не плодит дубли (проверка по паре alias+canonical+entity_type).
"""
import argparse
import json
import logging
import re
import sys
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("mine-terms")

# «средство защиты информации (СЗИ)», «автоматизированная система (АС)»
_ABBR = re.compile(r"(?P<long>[А-ЯЁ][а-яё]+(?:[ \u2011-][а-яё]+){1,7})\s*\((?P<abbr>[А-ЯЁ]{2,10})\)")
# «3.1 автоматизированная система: Система, состоящая из …»
_DEF = re.compile(r"^\s*\d+(?:\.\d+)*\s+(?P<term>[а-яё][^:\n]{2,60}?)\s*:\s+\S", re.M)

# Служебные слова, которые не являются термином документа
_STOP = {
    "примечание", "примечания", "пример", "примеры", "таблица", "рисунок", "сноска",
    "внимание", "итак", "кроме того", "например", "следовательно", "где", "если",
}

# Слова, с которых НЕ начинается термин: это начало фразы, а не название.
# Живой случай: «ИКТ → Из-за растущего числа информационно-коммуникационных технологий».
_BAD_START = {
    "из", "изза", "для", "при", "в", "во", "на", "с", "со", "по", "от", "к", "ко",
    "о", "об", "и", "а", "но", "что", "как", "это", "рост", "увеличение", "снижение",
    "применение", "использование", "наличие", "отсутствие", "возможность", "необходимость",
    "таким", "также", "тогда", "поэтому", "который", "которая", "которые", "его", "ее",
}


def _q(url: str, key: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "api-key": key})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def scroll_texts(limit_total: int = 40000) -> list:
    """Тексты фрагментов основной коллекции (документы, не новости)."""
    from src.config import get_settings

    cfg = get_settings()
    key = getattr(cfg, "QDRANT_API_KEY", "") or ""
    offset = None
    out = []
    url = "http://qdrant:6333/collections/kag_documents/points/scroll"
    while len(out) < limit_total:
        body = {"limit": 500, "with_payload": ["content", "text", "document_id", "filename"],
                "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        data = _q(url, key, body)
        result = data.get("result") or {}
        points = result.get("points") or []
        if not points:
            break
        for p in points:
            pl = p.get("payload") or {}
            text = pl.get("content") or pl.get("text") or ""
            if text:
                out.append({"text": text, "document_id": pl.get("document_id") or "",
                            "filename": pl.get("filename") or ""})
        offset = result.get("next_page_offset")
        if offset is None:
            break
        logger.info("прочитано фрагментов: %s", len(out))
    return out


def mine(chunks: list, max_pairs: int = 500, max_defs: int = 500) -> list:
    """Кандидаты: список (alias, canonical, kind, comment)."""
    pairs = {}
    pair_hits = {}
    defs = {}
    for ch in chunks:
        text = ch["text"]
        for m in _ABBR.finditer(text):
            long_form = " ".join(m.group("long").split()).strip(" -")
            abbr = m.group("abbr").strip()
            if len(long_form) < 12 or len(abbr) < 2:
                continue
            if long_form.upper().startswith(abbr):
                continue
            words = long_form.lower().split()
            if not (2 <= len(words) <= 6):
                continue
            if words[0] in _BAD_START:
                continue
            if " и " in long_form.lower():
                continue
            pair_hits[(abbr, long_form)] = pair_hits.get((abbr, long_form), 0) + 1
            pairs.setdefault((abbr, long_form), ch["filename"])
        for m in _DEF.finditer(text):
            term = " ".join(m.group("term").split()).strip(" -–—")
            if len(term) < 4 or term.lower() in _STOP:
                continue
            if term.count(" ") > 7:
                continue
            defs.setdefault(term, ch["filename"])

    # Пару берём, если она встретилась чаще одного раза: одноразовое совпадение — это
    # чаще всего обрывок фразы, а не расшифровка сокращения.
    ranked = sorted(((k, v) for k, v in pair_hits.items() if v >= 2),
                    key=lambda kv: (-kv[1], kv[0][0]))
    out = []
    for (abbr, long_form), hits in ranked[:max_pairs]:
        src = pairs.get((abbr, long_form), "")
        out.append((abbr, long_form, "термин", f"сокращение ({hits}×) из «{src[:60]}»"))
    for term, src in list(defs.items())[:max_defs]:
        out.append((term, term, "термин", f"определение из «{src[:60]}»"))
    return out


def save(candidates: list) -> tuple:
    session = None
    try:
        from src.database.entity_alias_models import EntityAlias
        from src.database.session import get_session_local

        session = get_session_local()()
        existing = {(r.alias, r.canonical_name) for r in
                    session.query(EntityAlias.alias, EntityAlias.canonical_name).all()}
        added = 0
        for alias, canonical, etype, comment in candidates:
            if (alias, canonical) in existing:
                continue
            session.add(EntityAlias(
                id=f"{abs(hash((alias, canonical))) & ((1 << 64) - 1):x}",
                alias=alias, canonical_name=canonical, entity_type="term",
                domain="infosec", source="extraction", comment=comment,
                reviewed=False, verdict="",
            ))
            existing.add((alias, canonical))
            added += 1
        session.commit()
        return added, len(candidates)
    finally:
        if session is not None:
            session.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40000, help="сколько фрагментов читать")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    chunks = scroll_texts(args.limit)
    logger.info("фрагментов прочитано: %s", len(chunks))
    cands = mine(chunks)
    logger.info("кандидатов найдено: %s", len(cands))
    pair_rows = [r for r in cands if "сокращение" in r[3]]
    def_rows = [r for r in cands if "определение" in r[3]]
    print("пар сокращений:", len(pair_rows), "| определений:", len(def_rows))
    for row in pair_rows[:10]:
        print("   пара:", row[0], "→", row[1])
    for row in def_rows[:10]:
        print("   термин:", row[0])
    if args.dry_run:
        print("dry-run: в базу не писали")
        return 0
    added, total = save(cands)
    print(f"добавлено в словарь: {added} из {total} (остальные уже были)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
