"""Сервис реранка для KAG: cross-encoder по HTTP (живёт на сервере моделей, напр. 41).

Зачем отдельный сервис, а не модель внутри api:
  * cross-encoder на 4 ядрах стенда добавляет 4–6 с к каждому вопросу, на 20 ядрах 41 — 1,5 с;
  * модель весит сотни мегабайт и её место рядом с другими моделями, а не в образе api;
  * на 41 сервис можно перезапускать и обновлять независимо от стенда.

Контракт (использует api):
    GET  /health                     -> {"status":"ok","model":"...","ready":true}
    POST /rerank {"query": str, "candidates":[{"id": str, "text": str}], "top_k": int}
         -> {"model": str, "took_ms": int, "scores":[{"id": str, "score": float}]}
    Порядок в ответе: от лучшего к худшему. Если candidate без текста — score = null.

Правила, важные для прода:
  * сервис НИКОГДА не должен ломать чат: любые внутренние ошибки — это HTTP 500, а api
    при недоступности обязан вернуть исходный порядок (проверка на стороне api);
  * модель грузится один раз при старте (прогрев), поэтому первый запрос не «съедает» таймаут;
  * /health отделяет «процесс жив» от «модель готова»: api не должен слать запросы, пока
    ready=false.

Запуск (на сервере моделей):
    HF_HOME=~/kag-eval/models ~/kag-eval/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8010
Переменные: RERANK_MODEL (по умолчанию DiTy/cross-encoder-russian-msmarco),
            RERANK_MAX_LENGTH=512, RERANK_THREADS=16, RERANK_CANDIDATE_CHARS=2000.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

MODEL_ID = os.environ.get("RERANK_MODEL", "DiTy/cross-encoder-russian-msmarco")
MAX_LENGTH = int(os.environ.get("RERANK_MAX_LENGTH", "512"))
THREADS = int(os.environ.get("RERANK_THREADS", "16"))
CAND_CHARS = int(os.environ.get("RERANK_CANDIDATE_CHARS", "2000"))
# Бэкенд: onnx (быстро, без torch — замер 26.09.2026: 120 мс против 1,46 с у torch на 10 парах)
# или st (sentence-transformers, torch). int8 НЕ используем: динамическое квантование сломало
# ранжирование (топ-1 сместился с правильного фрагмента на посторонний) — скорость без качества
# не нужна, поэтому берём fp32-ONNX.
BACKEND = os.environ.get("RERANK_BACKEND", "onnx")
ONNX_DIR = os.environ.get("RERANK_ONNX_DIR", os.path.expanduser("~/kag-eval/models/DiTy_cross-encoder-russian-msmarco-onnx"))

app = FastAPI(title="KAG rerank service", version="1.0.0")

_state: Dict[str, Any] = {"ready": False, "model": None, "error": None,
                          "loaded_at": None, "calls": 0}
_lock = threading.Lock()


class Candidate(BaseModel):
    id: str
    text: str = ""


class RerankRequest(BaseModel):
    query: str
    candidates: List[Candidate] = Field(default_factory=list)
    top_k: Optional[int] = None


def _load_model() -> None:
    """Загрузить модель один раз (прогрев). Ошибку запоминаем, чтобы /health объяснял причину.

    ONNX-путь: сессия onnxruntime + токенизатор. Это быстрее torch в 12 раз на наших 10 парах
    и не тянет torch в образ api (там onnxruntime уже есть для OCR).
    """
    try:
        if BACKEND == "onnx":
            import onnxruntime as ort
            from transformers import AutoTokenizer

            so = ort.SessionOptions()
            so.intra_op_num_threads = THREADS
            _state["session"] = ort.InferenceSession(
                os.path.join(ONNX_DIR, "model.onnx"), so, providers=["CPUExecutionProvider"])
            _state["tokenizer"] = AutoTokenizer.from_pretrained(ONNX_DIR)
            _state["input_names"] = [i.name for i in _state["session"].get_inputs()]
        else:
            import torch
            from sentence_transformers import CrossEncoder
            torch.set_num_threads(THREADS)
            _state["model"] = CrossEncoder(MODEL_ID, max_length=MAX_LENGTH)
        _state["ready"] = True
        _state["backend"] = BACKEND
        _state["loaded_at"] = time.time()
    except Exception as e:                      # noqa: BLE001 — сервис обязан стартовать и объяснить
        _state["error"] = f"{type(e).__name__}: {e}"
        _state["ready"] = False


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_load_model, daemon=True).start()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok" if _state["ready"] else "loading",
            "ready": bool(_state["ready"]),
            "model": MODEL_ID,
            "error": _state["error"],
            "calls": _state["calls"]}


@app.post("/rerank")
def rerank(req: RerankRequest) -> Dict[str, Any]:
    if not _state["ready"]:
        # 503, а не пустой список: api должен отличить «нет сервиса» от «сервис отсортировал так же».
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503,
                            content={"error": "модель не готова", "detail": _state["error"]})

    t0 = time.time()
    model = _state["model"]
    cands = [c for c in req.candidates]
    pairs, idx_with_text = [], []
    for i, c in enumerate(cands):
        text = (c.text or "")[:CAND_CHARS]
        if text.strip():
            pairs.append((req.query, text))
            idx_with_text.append(i)

    scores: List[Optional[float]] = [None] * len(cands)
    if pairs:
        with _lock:                              # инференс не потокобезопасен на CPU-памяти
            if _state.get("backend") == "onnx":
                import numpy as np
                tok = _state["tokenizer"]
                enc = tok([req.query] * len(pairs),
                          [t for _, t in pairs],
                          padding=True, truncation=True, max_length=MAX_LENGTH,
                          return_tensors="np")
                feed = {k: v for k, v in enc.items() if k in _state["input_names"]}
                out = _state["session"].run(None, feed)
                preds = [float(x) for x in np.array(out[0]).reshape(-1)]
            else:
                preds = model.predict([(q, t) for q, t in pairs], batch_size=8,
                                      show_progress_bar=False)
        for pos, i in enumerate(idx_with_text):
            scores[i] = round(float(preds[pos]), 6)

    order = sorted(range(len(cands)), key=lambda i: (scores[i] is not None, scores[i] or -1e9),
                   reverse=True)
    if req.top_k:
        order = order[:req.top_k]

    with _lock:
        _state["calls"] += 1
    return {"model": MODEL_ID,
            "took_ms": int((time.time() - t0) * 1000),
            "scores": [{"id": cands[i].id, "score": scores[i]} for i in order]}
