"""Ускорение cross-encoder'а для реранка: экспорт в ONNX и int8-квантование.

Зачем: DiTy на 20 ядрах даёт 1,46 с на вопрос (10 кандидатов, fp32 через torch). Для чата это
много. ONNX Runtime на CPU обычно в 2–4 раза быстрее, а динамическое int8-квантование снимает
ещё часть времени и памяти — и, что важно для нас, убирает torch из сервиса (в образе api
onnxruntime уже есть для OCR).

Что делает скрипт:
  1. экспортирует модель в ONNX (optimum) в ~/kag-eval/models/<имя>-onnx;
  2. делает int8-версию (onnxruntime.quantization, dynamic);
  3. замеряет время на 10 парах «вопрос-фрагмент» для fp32-ONNX и int8-ONNX и сравнивает с torch;
  4. проверяет, что порядок фрагментов не разъехался: сравнение ранжирования на реальном вопросе.

Запуск на сервере моделей:
    ~/kag-eval/venv/bin/python scripts/rerank_onnx_export.py --model DiTy/cross-encoder-russian-msmarco
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

CAND = [
    ("точно", "Требования к защите информации в финансовых организациях установлены "
              "ГОСТ Р 57580.1-2017 и определяют уровни защиты информации"),
    ("рядом", "Защита информации в финансовых организациях включает организационные и технические меры"),
    ("шум-1", "Порядок расчёта платы за отопление в жилом доме и нормативы потребления коммунальных услуг"),
    ("шум-2", "Инфляция в мае 2026 года составила 8,3 процента в годовом выражении"),
    ("шум-3", "Династия Рюриковичей прекратилась в 1598 году со смертью царя Фёдора Иоанновича"),
    ("шум-4", "Методические рекомендации Банка России по тестированию на проникновение"),
    ("шум-5", "Режимы работы блочных шифров определены в ГОСТ Р 34.13-2015"),
    ("шум-6", "Стресс-сценарии в рамках ВПОДК: требования нового шаблона Банка России"),
    ("шум-7", "Картину «Девочка с персиками» написал Валентин Серов в 1887 году"),
    ("шум-8", "Тарифы на содержание жилого помещения утверждены общим собранием собственников"),
]
QUERY = "какие требования к защите информации в финансовых организациях"


def bench_onnx(path: str, tok_dir: str, repeats: int = 3) -> tuple[float, list[float]]:
    """Замер ONNX-модели: медиана времени на 10 пар и сами оценки."""
    import numpy as np
    import onnxruntime as ort
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(tok_dir)
    so = ort.SessionOptions()
    so.intra_op_num_threads = int(os.environ.get("RERANK_THREADS", "16"))
    sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
    names = [i.name for i in sess.get_inputs()]

    enc = tok([QUERY] * len(CAND), [c[1] for c in CAND], padding=True, truncation=True,
              max_length=512, return_tensors="np")
    feed = {k: v for k, v in enc.items() if k in names}

    times = []
    scores: list[float] = []
    for _ in range(repeats):
        t0 = time.time()
        out = sess.run(None, feed)
        times.append(time.time() - t0)
        logits = out[0]
        scores = [float(x) for x in np.array(logits).reshape(-1)]
    return statistics.median(times), scores


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="DiTy/cross-encoder-russian-msmarco")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    safe = args.model.replace("/", "_")
    out_dir = Path(args.out or os.path.expanduser(f"~/kag-eval/models/{safe}-onnx"))
    out_dir.mkdir(parents=True, exist_ok=True)
    fp32 = out_dir / "model.onnx"
    int8 = out_dir / "model-int8.onnx"

    print(f"модель: {args.model}\nкаталог: {out_dir}", flush=True)

    if not fp32.exists():
        print("=== 1) экспорт в ONNX (fp32) ===", flush=True)
        from optimum.onnxruntime import ORTModelForSequenceClassification
        from transformers import AutoTokenizer
        model = ORTModelForSequenceClassification.from_pretrained(args.model, export=True)
        tok = AutoTokenizer.from_pretrained(args.model)
        model.save_pretrained(out_dir)
        tok.save_pretrained(out_dir)
        print(f"  экспортировано: {sorted(p.name for p in out_dir.iterdir())[:6]}", flush=True)
    else:
        print("=== 1) экспорт уже есть, пропускаю ===", flush=True)

    if not int8.exists():
        print("=== 2) int8-квантование (dynamic) ===", flush=True)
        from onnxruntime.quantization import QuantType, quantize_dynamic
        quantize_dynamic(str(fp32), str(int8), weight_type=QuantType.QInt8)
        print(f"  int8 готов: {int8.stat().st_size / 1e6:.1f} МБ "
              f"(fp32 {fp32.stat().st_size / 1e6:.1f} МБ)", flush=True)

    print("=== 3) замер времени (10 пар, медиана из 3) ===", flush=True)
    t32, s32 = bench_onnx(str(fp32), str(out_dir))
    t8, s8 = bench_onnx(str(int8), str(out_dir))
    print(f"  ONNX fp32: {t32*1000:.0f} мс | ONNX int8: {t8*1000:.0f} мс "
          f"(для сравнения torch fp32 на этих же 10 парах давал ~1,5 с)", flush=True)

    print("=== 4) проверка, что порядок не разъехался ===", flush=True)
    order32 = [CAND[i][0] for i in sorted(range(len(CAND)), key=lambda i: -s32[i])]
    order8 = [CAND[i][0] for i in sorted(range(len(CAND)), key=lambda i: -s8[i])]
    print(f"  fp32: {' > '.join(order32[:4])} ...", flush=True)
    print(f"  int8: {' > '.join(order8[:4])} ...", flush=True)
    print(f"  топ-1 совпал: {order32[0] == order8[0]} | совпал весь порядок: {order32 == order8}",
          flush=True)

    res = {"model": args.model, "onnx_fp32_ms": round(t32 * 1000),
           "onnx_int8_ms": round(t8 * 1000), "top1_same": order32[0] == order8[0],
           "order_same": order32 == order8,
           "sizes_mb": {"fp32": round(fp32.stat().st_size / 1e6, 1),
                        "int8": round(int8.stat().st_size / 1e6, 1)}}
    (out_dir / "bench.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  итог сохранён: {out_dir / 'bench.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
