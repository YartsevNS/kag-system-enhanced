"""Поиск пробелов в онтологии: что в текстах корпуса есть, а в схеме отсутствует.

Схема пополняется по фактам корпуса, но предложением, а не автоприменением.
Скрипт читает чанки из Qdrant, вытаскивает ОПРЕДЕЛЯЕМЫЕ термины («X — это Y»,
«под X понимается Y», пункты словаря «3.1.14 X: …»), отбрасывает покрытое схемой
и словарями, остаток складывает в очередь ontology_schema_gaps + markdown-отчёт.

Почему не частотные n-граммы: они дают «сети интернет» и «обязательный категория».
Определение в тексте — готовое имя понятия, это и есть кандидат в схему.

Запуск (в контейнере kag-api):
  python /tmp/schema_gap.py --limit 3000 --top 30 --min-count 2
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

NL = chr(10)

GENERIC = set("""
безопасность информации информационная информационный информационные защита
требования требование меры мера документ документы система системы контроль
обеспечение порядок случае основании соответствии помощью например также данных
данные информация сведения лицо лица организация организации российской федерации
россия году года числе целью которых который которая установленных установленные
настоящего настоящей пункт пункты раздел статьи статья
""".split())

DOMAIN_HINTS = (
    "защит", "безопасн", "информац", "доступ", "угроз", "уязвим", "контрол", "аудит",
    "инцидент", "сертифик", "криптограф", "аутентифик", "шифров", "парол", "сет",
    "сервер", "требова", "мер", "риск", "политик", "регламент", "инструкц", "категор",
    "объект", "субъект", "носител", "журнал", "событ", "монитор", "резерв", "восстанов",
    "целостн", "конфиденциальн", "подлинност", "нарушител", "модель", "периметр",
)

DEF_PATTERNS = (
    re.compile(r"([А-Яа-яЁё][А-Яа-яЁё\s\-]{3,60}?)\s*[—–]\s*(?:это\s+)?[а-яё]"),
    re.compile(r"под\s+([А-Яа-яЁё][А-Яа-яЁё\s\-]{3,60}?)\s+понима(?:ется|ют)"),
    re.compile(r"(?:^|" + NL + r")[ \t]*\d+(?:\.\d+)*\.?[ \t]+"
               r"([А-Яа-яЁё][А-Яа-яЁё \t\-]{3,60}?)[ \t]*[:—–]"),
)


def definitions(text: str) -> List[str]:
    """Определяемые термины из текста."""
    out: List[str] = []
    for rx in DEF_PATTERNS:
        for m in rx.finditer(text):
            term = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;:()").lower()
            words = term.split()
            if 1 <= len(words) <= 4 and len(term) >= 5:
                out.append(term)
    return out


def get_schema_terms() -> set:
    """Что уже покрыто схемой и словарём видов."""
    from src.indexing import document_kinds, typedb_facts

    covered = set()
    try:
        c = typedb_facts._cfg()
        drv = typedb_facts._driver()
        try:
            schema = drv.databases.get(c["db"]).schema()
        finally:
            drv.close()
        for line in schema.splitlines():
            m = re.match(r"\s*(?:entity|relation|attribute)\s+([\w\-]+)", line)
            if m:
                covered.add(m.group(1).replace("-", " ").lower())
    except Exception:  # noqa: BLE001
        pass
    for code in document_kinds.codes():
        covered.add(document_kinds.title(code).lower())
        covered.add(document_kinds.short(code).lower())
    covered |= {
        "документ", "редакция", "пункт", "требование", "мера", "мера защиты", "объект",
        "объект кии", "угроза", "уязвимость", "сертификат", "доказательство", "регулятор",
        "издатель", "документ компании", "нормативный документ", "текст", "название",
        "номер", "дата", "источник", "фрагмент",
    }
    return covered


def get_chunks(limit: int) -> List[str]:
    """Тексты чанков из Qdrant."""
    import urllib.request

    key = ""
    try:
        from src.config import get_settings

        key = getattr(get_settings(), "QDRANT_API_KEY", "") or ""
    except Exception:  # noqa: BLE001
        pass
    body = json.dumps({"limit": limit, "with_payload": ["text", "content"],
                       "with_vector": False}).encode()
    req = urllib.request.Request(
        "http://qdrant:6333/collections/kag_documents/points/scroll",
        data=body, headers={"Content-Type": "application/json", "api-key": key})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    out = []
    for p in (data.get("result") or {}).get("points") or []:
        pl = p.get("payload") or {}
        text = pl.get("text") or pl.get("content") or ""
        if text:
            out.append(text)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-count", type=int, default=2)
    a = ap.parse_args()

    covered = get_schema_terms()
    covered_stems = {c[:5] for c in covered}
    generic_stems = {w[:5] for w in GENERIC}

    texts = get_chunks(a.limit)
    print(f"чанков прочитано: {len(texts)}")

    counter: Counter = Counter()
    examples: Dict[str, List[str]] = defaultdict(list)
    for t in texts:
        for term in set(definitions(t)):
            counter[term] += 1
    print(f"определяемых терминов: {len(counter)}")

    def stem(w: str) -> str:
        return w[:5]

    def covered_already(term: str) -> bool:
        return any(stem(w) in covered_stems for w in term.split())

    def domain(term: str) -> bool:
        return any(h in term for h in DOMAIN_HINTS)

    def ok_phrase(term: str) -> bool:
        words = term.split()
        if stem(words[-1]) in generic_stems:
            return False
        return not all(stem(w) in generic_stems for w in words)

    ranked: List[Tuple[str, int]] = [
        (t, n) for t, n in counter.most_common(5000)
        if n >= a.min_count and domain(t) and not covered_already(t) and ok_phrase(t)
    ][:a.top]
    print(f"в отчёт: {len(ranked)}")

    for term, _n in ranked:
        for t in texts:
            low = t.lower()
            if term in low:
                s = re.sub(r"\s+", " ", t)
                i = s.lower().find(term)
                examples[term].append(s[max(0, i - 60):i + len(term) + 90])
                if len(examples[term]) >= 2:
                    break

    lines = ["# Пробелы в схеме: термины корпуса, которых нет в онтологии",
             "",
             f"Прочитано чанков: {len(texts)}. Кандидатов после фильтров: {len(ranked)}.",
             "Это предложения, а не решения: тип/атрибут добавляется через редактор онтологии.",
             ""]
    for term, n in ranked:
        lines.append(f"## {term} — встречается в {n} фрагментах")
        for ex in examples.get(term, [])[:2]:
            lines.append(f"> …{ex}…")
        lines.append("")
    open("/tmp/schema_gap_report.md", "w", encoding="utf-8").write(NL.join(lines))

    from sqlalchemy import text

    from src.database.session import get_session_local

    db = get_session_local()()
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS ontology_schema_gaps (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                term text NOT NULL UNIQUE,
                hits integer NOT NULL DEFAULT 0,
                examples jsonb NOT NULL DEFAULT '[]'::jsonb,
                status text NOT NULL DEFAULT 'proposed',
                reviewer text, reviewed_at timestamptz, note text,
                created_at timestamptz NOT NULL DEFAULT now()
            )"""))
        for term, n in ranked:
            db.execute(text("""
                INSERT INTO ontology_schema_gaps (term, hits, examples)
                VALUES (:t, :h, CAST(:e AS jsonb))
                ON CONFLICT (term) DO UPDATE SET hits = GREATEST(ontology_schema_gaps.hits, :h)
            """), {"t": term, "h": n,
                   "e": json.dumps(examples.get(term, [])[:2], ensure_ascii=False)})
        db.commit()
        total = db.execute(text("select count(*) from ontology_schema_gaps")).scalar()
    finally:
        db.close()
    print(f"в очереди ontology_schema_gaps: {total}")
    print("отчёт: /tmp/schema_gap_report.md")


if __name__ == "__main__":
    main()
