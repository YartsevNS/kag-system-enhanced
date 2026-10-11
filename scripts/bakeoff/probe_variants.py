"""Сравнение формулировок промпта на одних и тех же фрагментах.

МЕТРИКА (важно): считаем только содержательные цитаты (>= 15 знаков), не равные имени
source/target, и требуем найти их в тексте. Мягкая проверка принимала «УЗП.20» за цитату
и давала 100 процентов всем вариантам — такая метрика не различает формулировки.

    docker exec kag-api python /tmp/probe_variants.py <документ> <сколько_фрагментов>

Замер на вариант: связей, доля цитат, найденных в тексте, время, токены, кэш.
Без выкатки и без кэша приложения — прямой вызов модели.
"""

import asyncio
import json
import re
import sys
import time

import requests

DOC = sys.argv[1] if len(sys.argv) > 1 else "2873e687-d476-43e6-b94e-f4dd14eaead8"
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 3

from src.api.services.provider_service import provider_service  # noqa: E402

cfg = provider_service.get_function_llm_config("graph")
params = cfg.get("parameters") or {}

from src.indexing.embeddings_service import service_for_document  # noqa: E402

try:
    from src.indexing.graph_ontology import prompt_rules

    rel_desc = prompt_rules()[:2200]
except Exception:  # noqa: BLE001
    rel_desc = ""

chunks = asyncio.run(service_for_document(DOC).get_document_chunks(DOC))
step = max(1, len(chunks) // COUNT)
picked = [chunks[i] for i in range(0, len(chunks), step)][:COUNT]
print("фрагментов в документе:", len(chunks), "| к проверке:", len(picked))

HEAD = "Извлеки связи между сущностями. Только JSON без пояснений.\n\nТипы связей:\n" + rel_desc + "\n\n"

VARIANTS = {
    "A_текущая": """Текст:
---
{text}
---

JSON:
{{"relations":[{{"source":"...","target":"...","type":"тип связи","evidence":"дословная цитата из текста"}}]}}

Правила:
- выведи ВСЕ связи, которые явно есть в тексте; пустой список — только если связей нет совсем
- source и target: точные имена из текста
- evidence — ДОСЛОВНАЯ часть текста фрагмента (10-60 знаков). Если короткой цитаты не подобрать —
  поставь evidence: null, связь всё равно выведи""",
    "B_пример": """Пример верной пары:
{{"source":"ГОСТ Р 57580.1—2017","target":"ГОСТ Р 57580.1","type":"replaces","evidence":"вводится взамен ГОСТ Р 57580.1-2017"}}

Текст:
---
{text}
---

JSON:
{{"relations":[{{"source":"...","target":"...","type":"тип связи","evidence":"дословная цитата из текста"}}]}}

Правила:
- выведи ВСЕ связи, которые явно есть в тексте
- evidence — ДОСЛОВНАЯ часть текста фрагмента (10-60 знаков), скопированная без изменений;
  если подобрать не удалось — evidence: null""",
    "C_сначала_цитаты": """Шаг 1. Найди в тексте короткие дословные фразы, из которых видна связь между двумя сущностями.
Шаг 2. Для каждой такой фразы укажи тип связи из списка и имена source/target ровно как в тексте.

Текст:
---
{text}
---

JSON:
{{"relations":[{{"source":"...","target":"...","type":"тип связи","evidence":"дословная цитата из текста"}}]}}

Правила:
- evidence — ДОСЛОВНАЯ часть текста (10-60 знаков), копия без изменений; без цитаты связь не выводи
- выведи столько связей, сколько подтверждено цитатами (обычно 3-10)""",
    "D_минимум_связей": """Текст:
---
{text}
---

В тексте почти всегда есть не меньше 3 явных связей. Найди их.

JSON:
{{"relations":[{{"source":"...","target":"...","type":"тип связи","evidence":"дословная цитата из текста"}}]}}

Правила:
- evidence — ДОСЛОВНАЯ копия части текста (10-60 знаков); пересказ запрещён
- если связь очевидна, но цитату скопировать не удалось — evidence: null
- меньше 3 связей — только если текст действительно пустой по смыслу""",
}

url = (cfg.get("url") or "").rstrip("/")
if not url.endswith("/chat/completions"):
    url = url + "/chat/completions"
headers = {"Content-Type": "application/json"}
if cfg.get("api_key"):
    headers["Authorization"] = "Bearer " + cfg["api_key"]


def norm(x):
    return re.sub(r"\s+", " ", (x or "").lower()).strip()


print("\n вариант | связей | цитат>=15зн | найдено в тексте | доля | сек | токенов | из кэша")
for name, tpl in VARIANTS.items():
    REL = EV = FOUND = 0
    secs = 0.0
    tok = hit = 0
    for ch in picked:
        text = ch.get("text") or ch.get("content") or ""
        prompt = HEAD + tpl.format(text=text[:3500])
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
        t0 = time.time()
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=180)
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: СЕТЬ {type(e).__name__} {str(e)[:80]}")
            continue
        secs += time.time() - t0
        if r.status_code != 200:
            print(f"  {name}: код {r.status_code} {r.text[:80]}")
            continue
        data = r.json()
        u = data.get("usage") or {}
        tok += int(u.get("total_tokens") or 0)
        hit += int(u.get("prompt_cache_hit_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        body = content
        if "```" in body:
            body = re.sub(r"^.*?```(?:json)?", "", body, flags=re.S).split("```")[0]
        try:
            rels = (json.loads(body[body.find("{"):]) or {}).get("relations") or []
        except Exception:  # noqa: BLE001
            continue
        # Строгая проверка цитаты: учитываем только содержательные цитаты (>= 15 знаков),
        # не совпадающие с именем source/target, и требуем найти их в тексте фрагмента.
        # Мягкая проверка давала «100 процентов» на цитатах вида «УЗП.20» — это имя сущности,
        # а не подтверждение, и такая метрика обманывает.
        REL += len(rels)
        EV += sum(1 for x in rels if len(str(x.get("evidence") or "").strip()) >= 15)
        for x in rels:
            ev = norm(str(x.get("evidence") or ""))
            if len(ev) < 15:
                continue
            if ev == norm(str(x.get("source"))) or ev == norm(str(x.get("target"))):
                continue
            if ev[:60] in norm(text):
                FOUND += 1
    share = int(100 * FOUND / REL) if REL else 0
    cache = int(100 * hit / tok) if tok else 0
    print(f" {name:18} | {REL:6} | {EV:9} | {FOUND:15} | {share:3} % | {secs:5.0f} | {tok:7} | {cache:3} %")
