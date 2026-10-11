"""Щуп: один фрагмент -> один запрос связей -> СЫРОЙ ответ модели.

Без выкатки, без кэша, без пересборки. Задача — увидеть своими глазами, отдаёт ли
модель поле подтверждения (evidence), когда оно есть в образце ответа.

Запуск внутри контейнера api (там же настройки провайдера и сеть):
    docker exec kag-api python /tmp/probe_evidence.py [документ] [номер_фрагмента]
"""

import json
import os
import sys

import requests

DOC = sys.argv[1] if len(sys.argv) > 1 else "2873e687-d476-43e6-b94e-f4dd14eaead8"
WANT = sys.argv[2] if len(sys.argv) > 2 else "00051"

from src.api.services.provider_service import provider_service  # noqa: E402

cfg = provider_service.get_function_llm_config("graph")
if not isinstance(cfg, dict):
    print("КОНФИГ НЕ СЛОВАРЬ:", type(cfg).__name__, str(cfg)[:200])
    raise SystemExit(1)
params = cfg.get("parameters") or {}
print("ПРОВАЙДЕР:", cfg.get("provider"), "| модель:", cfg.get("model"))
print("URL:", (cfg.get("url") or "")[:60])
print("параметры привязки: no_think =", params.get("no_think", "по умолчанию True"),
      "| температура =", params.get("temperature", "по умолчанию 0.05"))

try:
    import asyncio as _aio

    from src.indexing.embeddings_service import service_for_document

    chunks = _aio.run(service_for_document(DOC).get_document_chunks(DOC))
except Exception as e:  # noqa: BLE001
    print("НЕ ВЗЯТЬ ФРАГМЕНТЫ:", type(e).__name__, e)
    raise SystemExit(2)

print("фрагментов в документе:", len(chunks))
target = None
for c in chunks:
    cid = str(c.get("chunk_id") or c.get("id") or "")
    if cid.endswith(WANT) or cid == WANT:
        target = c
        break
if target is None:
    print("не нашёл фрагмент", WANT, "| примеры:", [str(c.get("chunk_id"))[-5:] for c in chunks[:6]])
    raise SystemExit(2)
text = target.get("text") or target.get("content") or ""
print("фрагмент:", str(target.get("chunk_id"))[-12:], "| знаков:", len(text))

# Правила типов — берём те же, что в рабочем задании.
rel_desc = ""
try:
    from src.indexing.graph_ontology import prompt_rules

    rel_desc = prompt_rules()
except Exception as e:  # noqa: BLE001
    print("правила онтологии не взяты:", e)

prompt = f"""Извлеки связи между сущностями. Только JSON.

Типы связей:
{rel_desc[:3000]}

Текст:
---
{text[:4000]}
---

JSON:
{{"relations":[{{"source":"...","target":"...","type":"тип связи","evidence":"дословная цитата из текста"}}]}}

Правила:
- выведи ВСЕ связи, которые явно есть в тексте; пустой список — только если связей нет совсем
- source и target: точные имена из текста
- evidence — ДОСЛОВНАЯ часть текста фрагмента (10-60 знаков), подтверждающая связь.
  Приведи цитату для каждой связи; если связь явна, но короткой цитаты не подобрать —
  поставь evidence: null, связь всё равно выведи
- связи нет → {{"relations":[]}}"""

headers = {"Content-Type": "application/json"}
key = cfg.get("api_key") or ""
if key:
    headers["Authorization"] = "Bearer " + key
payload = {
    "model": cfg.get("model"),
    "messages": [
        {"role": "system", "content": "Ты — эксперт по извлечению структурированных данных. Отвечай строго в JSON."},
        {"role": "user", "content": prompt},
    ],
    "temperature": params.get("temperature", 0.05),
    "max_tokens": 1500,
}
# Отключаем размышления: без этого бюджет уходит в reasoning и ответ приходит пустым.
if params.get("no_think", True):
    payload["thinking"] = {"type": "disabled"}
    payload["enable_thinking"] = False

url = (cfg.get("url") or "").rstrip("/")
if not url.endswith("/chat/completions"):
    url = url + "/chat/completions"
print("отправляю запрос…")
try:
    r = requests.post(url, headers=headers, json=payload, timeout=180)
except Exception as e:  # noqa: BLE001
    print("СЕТЬ: не достучались:", type(e).__name__, str(e)[:300])
    raise SystemExit(3)
print("код ответа:", r.status_code)
if r.status_code != 200:
    print("тело:", r.text[:500])
    raise SystemExit(4)

data = r.json()
msg = (data.get("choices") or [{}])[0].get("message") or {}
content = msg.get("content") or ""
usage = data.get("usage") or {}
print("usage:", json.dumps(usage, ensure_ascii=False))
print("размышления (reasoning), знаков:", len(msg.get("reasoning_content") or ""))
print("=== СЫРОЙ ОТВЕТ (", len(content), "знаков ) ===")
print(content[:2500])
print("=== ПРИЗНАКИ ===")
print("  есть слово evidence:", "evidence" in content)
try:
    start = content.find("{")
    parsed = json.loads(content[start:])
    rels = parsed.get("relations") or []
    print("  связей в ответе:", len(rels))
    with_ev = [x for x in rels if str(x.get("evidence") or "").strip()]
    print("  из них с цитатой:", len(with_ev))
    for x in with_ev[:4]:
        print("    ", x.get("type"), "|", str(x.get("source"))[:28], "->", str(x.get("target"))[:28],
              "| цитата:", str(x.get("evidence"))[:60])
except Exception as e:  # noqa: BLE001
    print("  разбор JSON не удался:", type(e).__name__, str(e)[:120])
