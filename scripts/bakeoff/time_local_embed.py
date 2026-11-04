"""Замер локальной модели на 41: отдельно время загрузки и отдельно скорость счёта.

Вход (stdin, JSON): {"chunks": [{"chunk_id": "...", "text": "..."}, ...]}
Печатает: время загрузки модели, время счёта всех фрагментов, секунды на фрагмент
и расчёт на корпус (9531 фрагмент / 10000 документов).
"""

import json
import sys
import time

MODEL = "ai-sage/Giga-Embeddings-instruct"


def main():
    data = json.loads(sys.stdin.read() or "{}")
    texts = [str(c.get("text") or "") for c in (data.get("chunks") or [])]
    if not texts:
        print("нет текстов")
        return
    from sentence_transformers import SentenceTransformer

    t0 = time.time()
    m = SentenceTransformer(MODEL, trust_remote_code=True)
    m.max_seq_length = 4096
    t_load = time.time() - t0

    t1 = time.time()
    m.encode(texts, normalize_embeddings=True, batch_size=8, show_progress_bar=False)
    t_enc = time.time() - t1

    per = t_enc / len(texts)
    print(f"фрагментов в замере: {len(texts)}")
    print(f"загрузка модели: {t_load:.1f} с (один раз на процесс)")
    print(f"счёт {len(texts)} фрагментов: {t_enc:.1f} с")
    print(f"ОТДАЧА: {per:.2f} с на фрагмент")
    print(f"прогноз на корпус 9531 фрагмент: {9531 * per / 60:.0f} мин ({9531 * per / 3600:.1f} ч) при одном потоке счёта")
    print(f"прогноз на 10 000 документов (~200 000 фрагментов): {200000 * per / 3600:.0f} ч при одном потоке")


if __name__ == "__main__":
    main()
