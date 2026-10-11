"""Замер: сколько экономит ПАЧКА фрагментов в одном запросе против вызова на каждый фрагмент.

Зачем: на документе из 180 фрагментов одиночный режим даёт медиану 2,0 с на фрагмент при запросе
~1100 токенов — то есть накладные расходы вызова сопоставимы с полезной работой. Если отправить
несколько фрагментов одним запросом, платим за одну задержку вместо нескольких.

Прибор сравнивает на ОДНИХ И ТЕХ ЖЕ фрагментах:
  * режим «по одному» — ровно рабочая функция extract_chunk_v2 (эталон);
  * режим «пачкой»    — те же фрагменты в одном запросе, по 2 / 4 / 5 штук.
Метрика одна и та же: доля связей, чья дословная цитата найдена в СВОЁМ фрагменте. Считаем также
время, токены и ошибки разбора. Сырые ответы сохраняются в JSON.

    python /tmp/audit/audit_batch.py <документ> [сколько_фрагментов] [размер_пачки]
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time

DOC = sys.argv[1] if len(sys.argv) > 1 else "2873e687-d476-43e6-b94e-f4dd14eaead8"
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 10
PACK = int(sys.argv[3]) if len(sys.argv) > 3 else 4
OUT = sys.argv[4] if len(sys.argv) > 4 else f"/tmp/audit_batch_{PACK}.json"

import requests  # noqa: E402

from src.indexing.embeddings_service import service_for_document  # noqa: E402
from src.indexing.extraction_v2 import extract_chunk_v2  # noqa: E402
from src.indexing.graph_ontology import relations_compact  # noqa: E402

SYSTEM_PROMPT = "Ты — эксперт по извлечению структурированных данных. Отвечай строго в JSON."


def flat(x: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", (x or "").lower()).strip()


def batch_prompt(items: list[tuple[str, str]], rel_desc: str) -> str:
    """Один запрос на несколько фрагментов: каждый со своим номером, в ответе тот же номер."""
    body = "\n\n".join(f"### ФРАГМЕНТ {i}\n{text}" for i, (_, text) in enumerate(items, 1))
    return f"""Извлеки связи между сущностями в КАЖДОМ фрагменте. Только JSON, без пояснений.

Типы связей (type — только из этого списка):
{rel_desc}

Пример верной пары (так выглядит каждая связь):
{{"chunk":1,"source":"ГОСТ Р 57580.1—2017","target":"ГОСТ Р 57580.1","type":"replaces","evidence":"вводится взамен ГОСТ Р 57580.1-2017"}}

Шаг 1. Найди в каждом фрагменте короткие дословные фразы, из которых видна связь между сущностями.
Шаг 2. Для каждой фразы укажи номер фрагмента в поле chunk, тип связи из списка и имена source/target ровно как в тексте.

{body}

JSON:
{{"relations":[{{"chunk":номер фрагмента,"source":"...","target":"...","type":"тип связи","evidence":"дословная цитата из ЭТОГО фрагмента"}}]}}

Правила:
- выведи ВСЕ связи, которые явно есть в тексте; пустой список — только если связей нет совсем
- source и target: точные имена из текста; evidence — ДОСЛОВНАЯ часть текста (10-60 знаков), скопированная БЕЗ ИЗМЕНЕНИЙ
- если подобрать цитату не удалось — evidence: null, связь всё равно выведи
- поле chunk ОБЯЗАТЕЛЬНО: по нему связь привязывается к своему фрагменту"""


def parse(content: str) -> list[dict]:
    body = content
    if "```" in body:
        body = re.sub(r"^.*?```(?:json)?", "", body, flags=re.S).split("```")[0]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(body[start:end + 1])
    except Exception:  # noqa: BLE001
        return []
    return [x for x in (parsed.get("relations") or [])
            if x.get("source") and x.get("target") and x.get("chunk")]


def main() -> int:
    chunks = asyncio.run(service_for_document(DOC).get_document_chunks(DOC))
    step = max(1, len(chunks) // COUNT)
    picked = [chunks[i] for i in range(0, len(chunks), step)][:COUNT]
    rel_desc = relations_compact()

    from src.api.services.provider_service import provider_service
    cfg = provider_service.get_function_llm_config("graph")
    params = cfg.get("parameters") or {}
    url = (cfg.get("url") or "").rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]

    print("=" * 96)
    print(f"ЗАМЕР ПАЧКИ | документ {DOC} | фрагментов {len(picked)} | размер пачки {PACK}")
    print(f"провайдер: {cfg.get('provider')} / {cfg.get('model')} | список типов {len(rel_desc)} знаков")
    print("=" * 96)

    # ── Режим 1: по одному (эталон) ────────────────────────────────────────────
    rel1 = found1 = 0
    t1 = time.perf_counter()
    tok1 = {"prompt": 0, "cached": 0, "вывод": 0}
    for ch in picked:
        text = str(ch.get("content") or "")
        diag: dict = {}
        extract_chunk_v2(text, str(ch.get("chunk_id") or ""), diagnostics=diag)
        rels = diag.get("relations_model") or []
        tf = flat(text)
        rel1 += len(rels)
        found1 += sum(1 for r in rels
                      if flat(str(r.get("evidence") or "")) and flat(str(r["evidence"]))[:60] in tf)
        u = diag.get("usage") or {}
        tok1["prompt"] += int(u.get("prompt_tokens") or 0)
        tok1["cached"] += int(u.get("prompt_cache_hit_tokens") or 0)
        tok1["вывод"] += int(u.get("completion_tokens") or 0)
    sec1 = time.perf_counter() - t1
    print(f"\nРЕЖИМ «по одному»:  связей {rel1} | подтверждено {found1} "
          f"({int(100 * found1 / rel1) if rel1 else 0}%) | {sec1:.1f} с "
          f"({sec1 / len(picked):.1f} с на фрагмент) | токены: {tok1['prompt']} запрос "
          f"(кэш {tok1['cached']}), {tok1['вывод']} вывод")

    # ── Режим 2: пачками по PACK ───────────────────────────────────────────────
    packs = [picked[i:i + PACK] for i in range(0, len(picked), PACK)]
    rel2 = found2 = no_chunk = bad_json = 0
    t2 = time.perf_counter()
    tok2 = {"prompt": 0, "cached": 0, "вывод": 0}
    raw_dump = []
    for pi, pack in enumerate(packs, 1):
        items = [(str(c.get("chunk_id") or ""), str(c.get("content") or "")) for c in pack]
        prompt = batch_prompt(items, rel_desc)
        payload = {
            "model": cfg.get("model"),
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": prompt}],
            "temperature": params.get("temperature", 0.05),
            "max_tokens": 1600 * len(pack),
        }
        if params.get("no_think", True):
            payload["thinking"] = {"type": "disabled"}
            payload["enable_thinking"] = False
        t0 = time.perf_counter()
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=300)
        except Exception as e:  # noqa: BLE001
            print(f"  пачка {pi}: СЕТЬ {type(e).__name__}")
            continue
        sec = time.perf_counter() - t0
        if r.status_code != 200:
            print(f"  пачка {pi}: код {r.status_code} {r.text[:100]}")
            continue
        data = r.json()
        u = data.get("usage") or {}
        tok2["prompt"] += int(u.get("prompt_tokens") or 0)
        tok2["cached"] += int(u.get("prompt_cache_hit_tokens") or 0)
        tok2["вывод"] += int(u.get("completion_tokens") or 0)
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        rels = parse(content)
        if not rels:
            bad_json += 1
        per_pack_found = 0
        for r_ in rels:
            try:
                idx = int(r_.get("chunk"))
            except Exception:  # noqa: BLE001
                no_chunk += 1
                continue
            if idx < 1 or idx > len(items):
                no_chunk += 1
                continue
            tf = flat(items[idx - 1][1])
            ev = flat(str(r_.get("evidence") or ""))
            hit = bool(ev) and (ev[:60] in tf or ev in tf)
            rel2 += 1
            found2 += 1 if hit else 0
            per_pack_found += 1 if hit else 0
        print(f"  пачка {pi} ({len(pack)} фрагментов): связей {len(rels)} | подтверждено {per_pack_found} | "
              f"{sec:.1f} с | вывод {int(u.get('completion_tokens') or 0)} ток")
        raw_dump.append({"пачка": pi, "фрагментов": len(pack), "seconds": round(sec, 2),
                         "usage": u, "relations": rels, "raw": content})
    sec2 = time.perf_counter() - t2
    print(f"\nРЕЖИМ «пачкой по {PACK}»: связей {rel2} | подтверждено {found2} "
          f"({int(100 * found2 / rel2) if rel2 else 0}%) | {sec2:.1f} с "
          f"({sec2 / len(picked):.1f} с на фрагмент) | токены: {tok2['prompt']} запрос "
          f"(кэш {tok2['cached']}), {tok2['вывод']} вывод")
    print(f"  связей без поля chunk: {no_chunk} | пачек с пустым ответом: {bad_json}")
    print("\nСРАВНЕНИЕ")
    print(f"  время:   по одному {sec1:.1f} с  →  пачкой {sec2:.1f} с   "
          f"(выигрыш {100 * (sec1 - sec2) / max(sec1, 1e-9):.0f}%)")
    print(f"  связей:  по одному {rel1}  →  пачкой {rel2}")
    print(f"  кач-во:  {int(100 * found1 / rel1) if rel1 else 0}%  →  "
          f"{int(100 * found2 / rel2) if rel2 else 0}% подтверждённых")
    print(f"  токены:  запрос {tok1['prompt']} → {tok2['prompt']} | вывод {tok1['вывод']} → {tok2['вывод']}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"документ": DOC, "фрагментов": len(picked), "размер_пачки": PACK,
                   "по_одному": {"связей": rel1, "подтверждено": found1, "секунд": round(sec1, 1),
                                 "токены": tok1},
                   "пачкой": {"связей": rel2, "подтверждено": found2, "секунд": round(sec2, 1),
                              "токены": tok2, "без_chunk": no_chunk, "пустых_ответов": bad_json},
                   "ответы_пачками": raw_dump}, f, ensure_ascii=False, indent=2)
    print(f"\nотчёт: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
