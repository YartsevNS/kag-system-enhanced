"""Щуп по НЕСКОЛЬКИМ фрагментам: сколько связей и у скольких цитата реально есть в тексте.

Запуск внутри контейнера api (наши настройки провайдера и сеть):
    docker exec kag-api python /tmp/probe_chunks.py <документ> <сколько_фрагментов>

Без выкатки, без кэша, без пересборки. Метрика: доля связей, у которых цитата
найдена в тексте фрагмента (нижний регистр, схлопнутые пробелы).
"""

import asyncio
import json
import re
import sys

import requests

DOC = sys.argv[1] if len(sys.argv) > 1 else "2873e687-d476-43e6-b94e-f4dd14eaead8"
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 5

from src.api.services.provider_service import provider_service  # noqa: E402

cfg = provider_service.get_function_llm_config("graph")
params = cfg.get("parameters") or {}
print("ПРОВАЙДЕР:", cfg.get("provider"), "| модель:", cfg.get("model"),
      "| no_think:", params.get("no_think", True), "| температура:", params.get("temperature", 0.05))

from src.indexing.embeddings_service import service_for_document  # noqa: E402

try:
    from src.indexing.graph_ontology import prompt_rules

    rel_desc = prompt_rules()[:3000]
except Exception as e:  # noqa: BLE001
    rel_desc = ""
    print("правила онтологии не взяты:", e)

chunks = asyncio.run(service_for_document(DOC).get_document_chunks(DOC))
step = max(1, len(chunks) // COUNT)
picked = [chunks[i] for i in range(0, len(chunks), step)][:COUNT]
print("фрагментов в документе:", len(chunks), "| к проверке:", len(picked))


def norm(x):
    # Такая же мягкая нормализация, как теперь в конвейере: только буквы и цифры.
    # На распознанном тексте тире, кавычки и переносы не совпадали с цитатой модели,
    # из-за чего доля подтверждённых была 0 при непустых цитатах.
    return re.sub(r"[^0-9a-zа-яё]+", " ", (x or "").lower()).strip()


url = (cfg.get("url") or "").rstrip("/")
if not url.endswith("/chat/completions"):
    url = url + "/chat/completions"
headers = {"Content-Type": "application/json"}
if cfg.get("api_key"):
    headers["Authorization"] = "Bearer " + cfg["api_key"]

REL = EV = FOUND = BAD_JSON = 0
HIT = MISS = 0

for ch in picked:
    cid = str(ch.get("chunk_id") or "")
    text = ch.get("text") or ch.get("content") or ""
    prompt = f"""Извлеки связи между сущностями. Только JSON.

Типы связей:
{rel_desc}

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
    payload = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": "Ты — эксперт по извлечению структурированных данных. Отвечай строго в JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": params.get("temperature", 0.05),
        "max_tokens": 1500,
    }
    if params.get("no_think", True):
        payload["thinking"] = {"type": "disabled"}
        payload["enable_thinking"] = False
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
    except Exception as e:  # noqa: BLE001
        print(" ", cid[-8:], "СЕТЬ:", type(e).__name__, str(e)[:120])
        continue
    if r.status_code != 200:
        print(" ", cid[-8:], "код", r.status_code, r.text[:120])
        continue
    data = r.json()
    usage = data.get("usage") or {}
    HIT += int(usage.get("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    MISS += int(usage.get("prompt_cache_miss_tokens") or usage.get("prompt_tokens") or 0)
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    body = content
    if "```" in body:
        body = re.sub(r"^.*?```(?:json)?", "", body, flags=re.S).split("```")[0]
    try:
        parsed = json.loads(body[body.find("{"):])
        rels = parsed.get("relations") or []
    except Exception as e:  # noqa: BLE001
        BAD_JSON += 1
        print(" ", cid[-8:], "РАЗБОР НЕ УДАЛСЯ:", type(e).__name__,
              "| разметка:", "```" in content, "| знаков:", len(content))
        continue
    ev = sum(1 for x in rels if str(x.get("evidence") or "").strip())
    found = sum(1 for x in rels
                if norm(str(x.get("evidence") or "")) and norm(str(x.get("evidence")))[:60] in norm(text))
    REL += len(rels)
    EV += ev
    FOUND += found
    print(" ", cid[-8:], "связей", len(rels), "| с цитатой", ev, "| цитата есть в тексте", found)

print("=== ИТОГО ===")
print("  связей:", REL, "| с непустой цитатой:", EV, "| ЦИТАТА НАЙДЕНА В ТЕКСТЕ:", FOUND,
      "| не разобранных ответов:", BAD_JSON)
if REL:
    print("  ДОЛЯ ПОДТВЕРЖДЁННЫХ:", int(100 * FOUND / REL), "%")
print("  кэш провайдера: попаданий", HIT, "| мимо", MISS,
      "| из кэша:", (int(100 * HIT / (HIT + MISS)) if (HIT + MISS) else 0), "%")
