"""Собрать набор v3: вопросы владельца с эталонными ОТВЕТАМИ (проверены по корпусу).

Основа — scripts/eval_qa_v1.json (вопрос + эталонный ответ + источник, прислал владелец).
Цель (relevant_document_ids) — конкретный документ НАШЕГО корпуса, найденный проверкой по фразам
из эталонного ответа (см. reports/_scratch/check_regulator_targets.py и check_two_targets.py),
а не по названию источника: например, макропруденциальные надбавки (40 → 100%) лежат
в обзоре 2026 года (96d1f5e0), а не в издании 2022 года (570d711d).

Состав v3 = исторические 7 (comparability) + 20 «QA» + 3 сравнительных + 3 контрольных.
Отличие от v2: у вопросов есть эталонный ответ, поэтому набор годится и для оценки ОТВЕТА
(не только поиска).
"""
import json
from pathlib import Path

QA = Path("scripts/eval_qa_v1.json")
ID_MAP = Path("scripts/eval_id_map.json")
V2 = Path("scripts/eval_questions_v2.json")
V3 = Path("scripts/eval_questions_v3.json")

# источник владельца -> документ корпуса (проверено поиском по фразам эталонного ответа)
SOURCE_TO_DOC = {
    8: "d3c3f673", 9: "1d9dacc7", 10: "b7324ebf", 11: "19cdddfc", 12: "5586487b",
    13: "a1f2eb21", 14: "e11fd1cf", 15: "892c0e3b", 16: "21a9eaef", 17: "18413c28",
    18: "87e01848", 19: "29b672a9", 20: "ddb26a90", 21: "4737b0f0", 22: "927e02ea",
    23: "8fd08f5a", 24: "1e3b8be3", 25: "265a7d0c", 26: "96d1f5e0", 27: "e68bd3fb",
}
# почему цель отличается от буквального источника (для честности в notes)
TARGET_NOTES = {
    23: "в корпусе нет 340-ФЗ — вопросы о цифровом рубле отвечает аналитический доклад",
    26: "в корпусе нет Указания 6992-У: надбавки 40→100% найдены в обзоре 2026 (96d1f5e0), "
        "издание 2022 (570d711d) их не содержит",
    21: "письмо 23-20/1031 в корпусе представлено документом «Изменение требований к "
        "стресс-тестированию в рамках ВПОДК…»",
    25: "цель — методрекомендации 16.06.2026 (265a7d0c); тот же сюжет есть и в докладе "
        "«Цифровая финансовая система России»",
}


def main() -> None:
    id_map = json.loads(ID_MAP.read_text(encoding="utf-8"))
    qa = json.loads(QA.read_text(encoding="utf-8"))
    v2 = json.loads(V2.read_text(encoding="utf-8"))

    def full(prefix: str) -> str:
        got = id_map.get(prefix)
        if not got:
            raise SystemExit(f"нет полного id для {prefix}: обновите scripts/eval_id_map.json")
        return got

    questions = []
    for item in qa:
        qid = int(item["id"])
        prefix = SOURCE_TO_DOC[qid]
        note = f"источник владельца: {item.get('source')}"
        if qid in TARGET_NOTES:
            note += f" | {TARGET_NOTES[qid]}"
        questions.append({
            "id": qid,
            "query": item["question"],
            "expected_answer": item["answer"],
            "source": item.get("source"),
            "relevant_document_ids": [full(prefix)],
            "relevant_chunk_ids": [],
            "notes": note,
        })

    # исторический блок и сравнительные с контролем берём из v2 (там уже починены эталоны)
    legacy = [q for q in v2["questions"][: int(v2.get("legacy_count") or 7)]]
    comparatives = [q for q in v2["questions"]
                    if len(q.get("relevant_document_ids") or []) > 1 and not q.get("control")]
    controls = [q for q in v2["questions"] if q.get("control")]

    doc = {
        "description": (
            "Набор v3 (2026-09-13): исторические вопросы (сравнимость с прошлыми прогонами) + "
            "20 вопросов с эталонными ответами владельца + сравнительные + контрольные. "
            "Эталоны привязаны к конкретным документам корпуса, проверенным поиском по фразам "
            "из эталонных ответов."
        ),
        "k_values": v2.get("k_values") or [1, 3, 5, 10],
        "legacy_count": len(legacy),
        "qa_count": len(questions),
        "questions": legacy + questions + comparatives + controls,
    }
    V3.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    with_answer = sum(1 for q in doc["questions"] if q.get("expected_answer"))
    print(f"  v3: всего {len(doc['questions'])} вопросов "
          f"(исторических {len(legacy)}, QA {len(questions)}, сравнительных {len(comparatives)}, "
          f"контрольных {len(controls)})")
    print(f"  с эталонным ответом: {with_answer}")


if __name__ == "__main__":
    main()
