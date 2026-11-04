"""Локальные векторы на 41: Giga-Embeddings-instruct (то же семейство, что облачная GigaR).

Читает со stdin JSON:
    {"mode": "query"|"doc", "texts": ["...", "..."]}
Печатает JSON:
    {"model": "...", "dim": N, "vectors": [[...], ...]}

Для запросов (mode=query) добавляется инструкция — модель обучена инструктивному стилю,
и без неё качество поиска падает (это прямо в карточке модели). Документы кодируются как есть.
"""

import json
import sys

MODEL = "ai-sage/Giga-Embeddings-instruct"
INSTR = "Дан вопрос, необходимо найти абзац текста с ответом"


def main():
    data = json.loads(sys.stdin.read() or "{}")
    mode = data.get("mode", "doc")
    texts = [str(t) for t in (data.get("texts") or [])]
    if not texts:
        print(json.dumps({"error": "нет текстов"}, ensure_ascii=False))
        return
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL, trust_remote_code=True)
    model.max_seq_length = 4096
    if mode == "query":
        texts = [f"Instruct: {INSTR}\nQuery: {t}" for t in texts]
    vecs = model.encode(texts, normalize_embeddings=True, batch_size=8, show_progress_bar=False)
    print(json.dumps({
        "model": MODEL,
        "mode": mode,
        "dim": int(len(vecs[0])),
        "vectors": [[round(float(x), 6) for x in v] for v in vecs],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
