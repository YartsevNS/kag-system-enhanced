"""Параллельный счёт локальных векторов на 41: N процессов, батч на процесс.

Задача: понять реальную отдачу на ядро и общую, чтобы оценить переиндексацию.
Вход (stdin, JSON): {"chunks": [{"chunk_id","text"}, ...]} — набор повторяется до нужного объёма.

    ~/embvenv/bin/python ~/par_embed.py 8 32 128 < файл.json
         (процессов, батч на процесс, сколько фрагментов всего считать)
"""

import json
import os
import sys
import time
from multiprocessing import Pool

MODEL = "ai-sage/Giga-Embeddings-instruct"


def worker(args):
    pid, texts, batch = args
    from sentence_transformers import SentenceTransformer

    m = SentenceTransformer(MODEL, trust_remote_code=True)
    m.max_seq_length = 4096
    t0 = time.time()
    m.encode(texts, normalize_embeddings=True, batch_size=batch, show_progress_bar=False)
    return pid, len(texts), time.time() - t0


def main():
    procs = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    batch = int(sys.argv[2]) if len(sys.argv) > 2 else 32
    total = int(sys.argv[3]) if len(sys.argv) > 3 else 128
    data = json.loads(sys.stdin.read() or "{}")
    base = [str(c.get("text") or "") for c in (data.get("chunks") or [])]
    if not base:
        print("нет текстов")
        return
    texts = [base[i % len(base)] for i in range(total)]
    per_worker = max(1, total // procs)
    jobs = []
    for i in range(procs):
        part = texts[i * per_worker:(i + 1) * per_worker]
        if part:
            jobs.append((i, part, batch))
    print(f"процессов: {len(jobs)} | батч на процесс: {batch} | всего фрагментов: {sum(len(j[1]) for j in jobs)}")
    print(f"ядер в системе: {os.cpu_count()}")
    t0 = time.time()
    with Pool(processes=len(jobs)) as pool:
        res = pool.map(worker, jobs)
    wall = time.time() - t0
    done = sum(r[1] for r in res)
    print("по процессам (номер, фрагментов, секунд):", [(r[0], r[1], round(r[2], 1)) for r in res])
    print(f"ВСЕГО: {done} фрагментов за {wall:.1f} с => {done / wall:.2f} фрагмента в секунду")
    print(f"отдача на фрагмент: {wall / done:.2f} с")
    print(f"прогноз на корпус 9531 фрагмент: {9531 / (done / wall) / 60:.0f} мин")
    print(f"прогноз на 200 000 фрагментов: {200000 / (done / wall) / 3600:.1f} ч")


if __name__ == "__main__":
    main()
