"""A/B/C по отбору фрагментов (min_score_gap) — запускается НА СТЕНДЕ.

Что делает: по очереди ставит значение отсечения в привязку функции chat, прогоняет один и тот же
эталонный набор вопросов, сохраняет ответы и контексты. В конце ОБЯЗАТЕЛЬНО возвращает привязку
к исходной (иначе стенд останется с экспериментальной настройкой).

Почему через привязку, а не через параметр запроса: значение из запроса зажато пользовательским
диапазоном 0,02…0,12, а 0 (выключено) и 0,15 (жёстко) доступны только админу (предел 0,30).

Запуск:
    docker exec kag-api python /app/data/eval_ab_cutoff.py

Результат: /app/data/collected_ab_<метка>.json по каждому варианту.
"""
import json
import os
import sys
import time
import urllib.request

API = os.environ.get("KAG_API", "http://localhost:8000")
VARIANTS = [("off", 0.0), ("cur", 0.03), ("agg", 0.15)]


def _req(method: str, path: str, token: str = "", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def login() -> str:
    pw = ""
    for path in ("/tmp/.kag_pass", "/app/data/.kag_pass"):
        try:
            pw = open(path).read().strip()
            break
        except Exception:
            continue
    if not pw:
        pw = os.environ.get("ADMIN_PASSWORD", "")
    d = _req("POST", "/api/v1/auth/login",
             payload={"username": os.environ.get("ADMIN_USER", "admin"), "password": pw})
    return d.get("access_token") or d.get("token") or ""


def get_map(token: str) -> dict:
    d = _req("GET", "/api/v1/admin/models/functions/chat", token)
    return d.get("mapping") or d


def set_gap(token: str, mapping: dict, value: float) -> None:
    params = dict(mapping.get("parameters") or {})
    params["min_score_gap"] = value
    body = {
        "function": "chat",
        "provider_id": mapping.get("provider_id") or "",
        "model": mapping.get("model") or "",
        "system_prompt": mapping.get("system_prompt") or "",
        "parameters": params,
        "fallback_provider_id": mapping.get("fallback_provider_id") or "",
        "fallback_model": mapping.get("fallback_model") or "",
    }
    _req("POST", "/api/v1/admin/models/functions", token, body)


def collect(token: str, items: list[dict], label: str) -> str:
    out = f"/app/data/collected_ab_{label}.json"
    rows = []
    for i, item in enumerate(items, 1):
        q = item["question"]
        ans = _req("POST", "/api/v1/chat/", token,
                   {"messages": [{"role": "user", "content": q}], "context_limit": 10})
        answer = ans.get("response") or ""
        ctx = []
        try:
            srch = _req("POST", "/api/v1/chat/search", token, {"query": q, "limit": 10})
            for c in (srch.get("chunks") or []):
                ctx.append({"id": c.get("id") or c.get("chunk_id"),
                            "filename": c.get("filename") or c.get("source_file"),
                            "score": c.get("score"),
                            "content": (c.get("content") or "")[:4000]})
        except Exception as e:
            print(f"    контексты не собраны: {e}", flush=True)
        rows.append({"id": item["id"], "question": q, "answer": answer,
                     "sources": [{"filename": s.get("filename") or s.get("source_file"),
                                  "score": s.get("score")} for s in (ans.get("sources") or [])],
                     "contexts": ctx,
                     "fragments_in_prompt": (ans.get("metadata") or {}).get("sources_count")})
        frag = rows[-1]["fragments_in_prompt"]
        print(f"    {i}/{len(items)} {item['id']}: {len(answer)} симв., фрагментов в промпте {frag}",
              flush=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return out


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/app/data/golden_dataset.jsonl"
    items = [json.loads(l) for l in open(src, encoding="utf-8") if l.strip()]
    token = login()
    print(f"вход: {'ок' if token else 'НЕ УДАЛСЯ'} | вопросов: {len(items)}", flush=True)

    original = get_map(token)
    orig_params = dict(original.get("parameters") or {})
    print(f"исходная привязка: provider={original.get('provider_id')} model={original.get('model')} "
          f"min_score_gap={orig_params.get('min_score_gap')}", flush=True)

    try:
        for label, value in VARIANTS:
            print(f"\n=== ВАРИАНТ {label}: min_score_gap={value} ===", flush=True)
            set_gap(token, original, value)
            time.sleep(3)          # даём кэшу настроек обновиться
            path = collect(token, items, label)
            print(f"  сохранено: {path}", flush=True)
    finally:
        set_gap(token, original, float(orig_params.get("min_score_gap") or 0.0))
        time.sleep(2)
        back = get_map(token)
        print(f"\nпривязка возвращена: min_score_gap="
              f"{dict(back.get('parameters') or {}).get('min_score_gap')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
