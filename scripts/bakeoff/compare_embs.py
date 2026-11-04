"""Сравнение моделей векторов на ОДНОМ вопросе и одних фрагментах.

Вход (stdin, JSON):
    {"question": "...", "cloud_top": ["chunk_id", ...], "chunks": [{"chunk_id": "...", "text": "..."}]}
Где cloud_top — порядок, который дала ОБЛАЧНАЯ модель (сначала лучший).

Что делает: считает локальные векторы (Giga-Embeddings-instruct, инструкция к запросу,
документы как есть), сортирует фрагменты по косинусной близости к вопросу и печатает
свой порядок рядом с облачным, плюс метрику согласия: сколько из облачного топа-3
осталось в локальном топе-3 и где оказался облачный лидер.

    ~/embvenv/bin/python ~/compare_embs.py < файл.json
"""

import json
import sys

MODEL = "ai-sage/Giga-Embeddings-instruct"
INSTR = "Дан вопрос, необходимо найти абзац текста с ответом"


def main():
    data = json.loads(sys.stdin.read() or "{}")
    q = str(data.get("question") or "").strip()
    chunks = data.get("chunks") or []
    cloud = [str(x) for x in (data.get("cloud_top") or [])]
    if not q or not chunks:
        print(json.dumps({"error": "нужны question и chunks"}, ensure_ascii=False))
        return
    from sentence_transformers import SentenceTransformer

    m = SentenceTransformer(MODEL, trust_remote_code=True)
    m.max_seq_length = 4096
    qv = m.encode([f"Instruct: {INSTR}\nQuery: {q}"], normalize_embeddings=True,
                  show_progress_bar=False)[0]
    ids = [str(c.get("chunk_id")) for c in chunks]
    texts = [str(c.get("text") or "") for c in chunks]
    dv = m.encode(texts, normalize_embeddings=True, batch_size=4, show_progress_bar=False)
    sims = []
    for i, v in enumerate(dv):
        s = sum(float(a) * float(b) for a, b in zip(qv, v))  # векторы нормированы -> косинус
        sims.append((ids[i], round(s, 4)))
    sims.sort(key=lambda x: x[1], reverse=True)
    local = [i for i, _ in sims]
    top3_cloud = cloud[:3]
    top3_local = local[:3]
    agree = len(set(top3_cloud) & set(top3_local))
    print("модель:", MODEL, "| размерность:", len(qv), "| фрагментов:", len(chunks))
    print("облачный порядок:", " > ".join(x[-8:] for x in cloud) or "(нет)")
    print("локальный порядок:", " > ".join(x[-8:] for x in local))
    print("близость по локальной модели:", ", ".join(f"{i[-8:]}={s}" for i, s in sims))
    print("СОГЛАСИЕ: из облачного топа-3 осталось в локальном топе-3:", agree, "из 3")
    if cloud:
        pos = local.index(cloud[0]) + 1 if cloud[0] in local else -1
        print("место облачного лидера в локальном порядке:", pos)


if __name__ == "__main__":
    main()
