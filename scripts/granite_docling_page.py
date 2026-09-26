"""Granite-Docling-258M: извлекаем СТРУКТУРУ страницы (DocTags → markdown).

Зачем именно так: Qwen2-VL-2B отлично читает компактные картинки, но на полной странице А4
(1241×1754) выдала мусор — похоже, упирается в бюджет визуальных токенов. Гипотеза: структуру
страницы лучше берёт специализированная модель (Granite-Docling: layout/таблицы/порядок чтения),
а текст — наш OCR, который русский читает корректно (в квитанциях и перечнях приборов он дал и
названия, и числа). Тогда таблица = структура от Docling + текст из OCR.

Модель англоязычная по карточке, поэтому её собственный текст ожидаемо испорчен — важно увидеть
именно КАРКАС (сколько строк/колонок, где шапка), а не её распознавание.

Запуск на сервере моделей:
    ~/kag-eval/venv/bin/python scripts/granite_docling_page.py --image vlm/receipt_page1.png
"""
from __future__ import annotations

import argparse
import time

import torch
from PIL import Image
from transformers import AutoModelForVision2Seq, AutoProcessor

MODEL = "ibm-granite/granite-docling-258M"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--max-new", type=int, default=3000)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    t0 = time.time()
    proc = AutoProcessor.from_pretrained(MODEL)
    model = AutoModelForVision2Seq.from_pretrained(MODEL, torch_dtype=torch.float32)
    model.eval()
    print(f"  загрузка модели: {time.time() - t0:.1f} с", flush=True)

    img = Image.open(args.image).convert("RGB")
    print(f"  изображение: {img.size[0]}x{img.size[1]} px", flush=True)

    messages = [{"role": "user", "content": [{"type": "image"},
                                            {"type": "text", "text": "Convert this page to docling."}]}]
    prompt = proc.apply_chat_template(messages, add_generation_prompt=True)
    inputs = proc(text=prompt, images=[img], return_tensors="pt")

    t1 = time.time()
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=args.max_new)
    dt = time.time() - t1
    tags = proc.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=False)[0].lstrip()

    print(f"  генерация: {dt:.1f} с | символов DocTags: {len(tags)}", flush=True)
    print("=== DocTags (первые 1500) ===", flush=True)
    print(tags[:1500])

    md = ""
    try:
        from docling_core.types.doc import DoclingDocument
        from docling_core.types.doc.document import DocTagsDocument
        doc = DoclingDocument.load_from_doctags(
            DocTagsDocument.from_doctags_and_image_pairs([tags], [img]), document_name="page")
        md = doc.export_to_markdown()
        print("=== Markdown (первые 2000) ===", flush=True)
        print(md[:2000])
    except Exception as e:                       # конвертация не критична для вывода
        print(f"  конвертация DocTags→markdown не удалась: {type(e).__name__}: {e}", flush=True)

    if args.out:
        open(args.out, "w", encoding="utf-8").write(f"# DocTags\n{tags}\n\n# Markdown\n{md}")
        print(f"  сохранено: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
