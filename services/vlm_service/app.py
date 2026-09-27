"""Сервис распознавания страницы в markdown с сохранением таблиц (Qwen2-VL, CPU).

Зачем сервис, а не скрипт: распознавание должно запускаться по одному фрагменту в момент обработки
документа и именно тогда, когда маршрутизатор страницы (src/indexing/page_router.py) этого потребовал.
Модель живёт на сервере моделей (41), потому что на стенде нет столько ядер и памяти.

Ключевые свойства:
  * модель грузится один раз в фоне при старте; /health отвечает сразу и говорит, готов ли сервис;
  * распознавание — по одному запросу за раз (CPU: параллельные вызовы только мешают друг другу);
  * можно передать область (bbox) — тогда модель получает не всю страницу, а фрагмент с таблицей.
    Это важно: замер 26.09.2026 показал, что по компактному фрагменту качество заметно выше, чем по
    целой странице А4 (и это быстрее, потому что меньше пикселей на входе);
  * ничего наружу не отправляется: модель и изображение остаются внутри контура.

Запуск (systemd-юнит пользователя на 41, порт 8011):
    ~/kag-eval/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8011
"""
from __future__ import annotations

import io
import os
import threading
import time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

MODEL_NAME = os.environ.get("VLM_MODEL", "Qwen/Qwen2-VL-2B-Instruct")
MAX_NEW_TOKENS = int(os.environ.get("VLM_MAX_NEW", "4096"))
THREADS = int(os.environ.get("VLM_THREADS", "16"))
MAX_IMAGE_SIDE = int(os.environ.get("VLM_MAX_IMAGE_SIDE", "1600"))

DEFAULT_PROMPT = (
    "Преобразуй изображение страницы в markdown. Таблицу оформи как markdown-таблицу: сохрани все "
    "строки, колонки и значения, ничего не пропускай, числа не теряй и не переставляй. "
    "Отвечай только разметкой, без пояснений и без вступлений."
)

app = FastAPI(title="KAG VLM service", version="1.0")
_lock = threading.Lock()
_state: dict = {"ready": False, "error": "", "loaded_in": 0.0, "calls": 0}
_processor = None
_model = None


def _load_model() -> None:
    """Загрузка модели в фоне: сервис отвечает на /health с первой секунды."""
    global _processor, _model
    t0 = time.time()
    try:
        import torch
        from transformers import AutoModelForVision2Seq, AutoProcessor

        torch.set_num_threads(THREADS)
        _processor = AutoProcessor.from_pretrained(MODEL_NAME)
        _model = AutoModelForVision2Seq.from_pretrained(MODEL_NAME, dtype=torch.float32)
        _model.eval()
        _state["ready"] = True
        _state["loaded_in"] = round(time.time() - t0, 1)
        print(f"[vlm] модель готова за {_state['loaded_in']} с: {MODEL_NAME}", flush=True)
    except Exception as e:  # noqa: BLE001 — причина видна в /health, сервис не падает молча
        _state["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"[vlm] не удалось загрузить модель: {_state['error']}", flush=True)


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_load_model, daemon=True).start()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "ready": _state["ready"],
        "model": MODEL_NAME,
        "threads": THREADS,
        "loaded_in_seconds": _state["loaded_in"],
        "calls": _state["calls"],
        "error": _state["error"],
    }


def _prepare_image(raw: bytes, box: str) -> Image.Image:
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if box:
        try:
            x0, y0, x1, y1 = (int(float(v)) for v in box.split(","))
        except Exception:
            raise HTTPException(status_code=400, detail="bbox должен быть «x0,y0,x1,y1»")
        img = img.crop((x0, y0, x1, y1))
    if max(img.size) > MAX_IMAGE_SIDE:
        ratio = MAX_IMAGE_SIDE / max(img.size)
        img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
    return img


@app.post("/recognize")
async def recognize(file: UploadFile = File(...), prompt: str = Form(DEFAULT_PROMPT),
                    bbox: str = Form("")) -> JSONResponse:
    if not _state["ready"]:
        # Честный отказ вместо ожидания: вызывающий код решает, ждать ему или обойтись без модели.
        raise HTTPException(status_code=503, detail=f"модель не готова: {_state['error'] or 'загружается'}")

    import torch

    img = _prepare_image(await file.read(), bbox)
    with _lock:                      # CPU: по одному распознаванию за раз
        t0 = time.time()
        messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}]
        text = _processor.apply_chat_template(messages, add_generation_prompt=True)
        inputs = _processor(text=[text], images=[img], return_tensors="pt")
        with torch.no_grad():
            out = _model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS)
        generated = out[:, inputs["input_ids"].shape[1]:]
        md = _processor.batch_decode(generated, skip_special_tokens=True)[0]
        seconds = round(time.time() - t0, 1)
        _state["calls"] += 1

    table_lines = sum(1 for line in md.splitlines() if line.strip().startswith("|"))
    return JSONResponse({
        "markdown": md,
        "seconds": seconds,
        "chars": len(md),
        "table_lines": table_lines,
        "image_px": f"{img.width}x{img.height}",
        "model": MODEL_NAME,
    })
