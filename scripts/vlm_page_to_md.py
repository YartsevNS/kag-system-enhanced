"""Локальное распознавание страницы в markdown с сохранением таблиц (Qwen2-VL).

Зачем: для картинок и сканов наш пайплайн даёт плоский текст в порядке чтения, и табличный слой
не может собрать из него таблицу. VLM получает изображение и возвращает разметку — её принимает
существующий табличный слой (document_tables/table_rows), то есть чат и SQL-вычисления работают
без переделок.

Запуск на сервере моделей (CPU):
    ~/kag-eval/venv/bin/python scripts/vlm_page_to_md.py --image path/to/page.png
Ничего наружу не отправляется: модель и картинка остаются локально.
"""
from __future__ import annotations

import argparse
import time

import torch
from PIL import Image
from transformers import AutoModelForVision2Seq, AutoProcessor

DEFAULT_PROMPT = (
    "Преобразуй изображение страницы в markdown. Таблицу оформи как markdown-таблицу: сохрани все "
    "строки, колонки и значения, ничего не пропускай, числа не теряй и не переставляй. "
    "Отвечай только разметкой, без пояснений и без вступлений."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--model", default="Qwen/Qwen2-VL-2B-Instruct")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--max-new", type=int, default=4096)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    t0 = time.time()
    print(f"  модель: {args.model}", flush=True)
    proc = AutoProcessor.from_pretrained(args.model)
    model = AutoModelForVision2Seq.from_pretrained(args.model, torch_dtype=torch.float32)
    model.eval()
    print(f"  загрузка модели: {time.time() - t0:.1f} с", flush=True)

    img = Image.open(args.image).convert("RGB")
    print(f"  изображение: {img.size[0]}x{img.size[1]} px", flush=True)

    messages = [{"role": "user", "content": [{"type": "image"},
                                            {"type": "text", "text": args.prompt}]}]
    text = proc.apply_chat_template(messages, add_generation_prompt=True)
    inputs = proc(text=[text], images=[img], return_tensors="pt")

    t1 = time.time()
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=args.max_new)
    dt = time.time() - t1
    gen = out[:, inputs["input_ids"].shape[1]:]
    md = proc.batch_decode(gen, skip_special_tokens=True)[0]

    print(f"  генерация: {dt:.1f} с | символов: {len(md)} | строк: {md.count(chr(10)) + 1}", flush=True)
    lines = [l for l in md.splitlines() if l.strip().startswith("|")]
    print(f"  строк markdown-таблицы: {len(lines)}", flush=True)
    print("=== РЕЗУЛЬТАТ ===", flush=True)
    print(md)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(md)
        print(f"  сохранено: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
