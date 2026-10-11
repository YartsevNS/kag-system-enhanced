"""Сравнение трёх режимов извлечения: два вызова на фрагмент / один вызов / пачка фрагментов.

Зачем: граф стоит денег и времени на каждый вызов модели. Надо понять, сколько можно экономить,
не теряя точность смыслов и не ломая привязку факта к фрагменту.

Режимы (задание одно и то же, меняется только способ отправки):
  A — как сейчас: сущности отдельным вызовом, связи отдельным (2 вызова на фрагмент);
  B — один совмещённый вызов на фрагмент (так делает LightRAG);
  C — пачка из N фрагментов в одном вызове, ответ с разметкой по фрагментам.

Что меряем по каждому режиму:
  * сколько связей извлечено и сколько сущностей;
  * сколько связей получили точный тип и сколько ушло в «связано с»;
  * сколько отбросил словарь (пары и типы проверяются нашей онтологией);
  * число вызовов модели и время — то, ради чего всё затевается;
  * для режима C отдельно: не перепутала ли модель фрагменты (имена из связи должны
    встречаться именно в том фрагменте, которому связь приписана).

Запуск в контейнере api:
    docker exec kag-api python /app/data/compare_batch_modes.py --chunks 12 --batch 3
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, "/app")

from qdrant_client import QdrantClient  # noqa: E402


def log(*a):
    print(*a, flush=True)


ap = argparse.ArgumentParser()
ap.add_argument("--chunks", type=int, default=12, help="сколько фрагментов взять для опыта")
ap.add_argument("--batch", type=int, default=3, help="сколько фрагментов в одном вызове в режиме C")
ap.add_argument("--doc", default="2873e687-d476-43e6-b94e-f4dd14eaead8",
                help="id документа (по умолчанию ГОСТ 57580 — фрагменты нашей пятёрки)")
args = ap.parse_args()


async def main() -> int:
    from src.indexing.entity_extractor import entity_extractor
    from src.api.services.provider_service import provider_service
    from src.indexing.graph_ontology import (
        ENTITY_TYPES, normalize_relation, prompt_rules, validate_triple,
    )

    cfg = provider_service.get_function_llm_config("graph") or {}
    model = cfg.get("model") or ""
    url = cfg.get("url") or cfg.get("base_url") or ""
    key = cfg.get("api_key") or ""
    provider = cfg.get("provider") or "deepseek"
    log(f"модель: {provider}/{model} | фрагментов: {args.chunks} | пачка в режиме C: {args.batch}")

    # Фрагменты берём ЧЕРЕЗ СЛУЖБУ документов, а не сырым запросом в векторную базу:
    # сырой scroll возвращает первые попавшиеся точки, и на нужном документе их оказывалось
    # две вместо двенадцати — замер на таком материале ничего не значит.
    from src.indexing.embeddings_service import embeddings_service

    chunks: list[dict] = []
    got = await embeddings_service.get_document_chunks(args.doc)
    for ch in got:
        text = str(ch.get("content") or "")
        if len(text) < 400:
            continue
        chunks.append({"id": str(ch.get("chunk_id") or ""), "text": text, "doc": args.doc})
        if len(chunks) >= args.chunks:
            break
    if not chunks:
        log("фрагментов не нашлось")
        return 1
    log(f"взято фрагментов: {len(chunks)} (один документ: {len({x['doc'] for x in chunks})})")

    types_line = ", ".join(f"{k} ({v})" for k, v in ENTITY_TYPES.items())
    rules = prompt_rules()

    def task_entities(text: str) -> str:
        return (f"Извлеки СУЩНОСТИ из текста.\nТИПЫ: {types_line}\n"
                f"Ответ — JSON: {{\"entities\":[{{\"name\":\"...\",\"type\":\"...\"}}]}}\n\nТЕКСТ:\n{text}")

    def task_relations(text: str, ents: list) -> str:
        names = ", ".join(str(e.get("name")) for e in ents[:60])
        return (f"{rules}\n\nИзвестные сущности: {names}\n"
                f"Ответ — JSON: {{\"relations\":[{{\"source\":\"...\",\"type\":\"...\",\"target\":\"...\"}}]}}\n"
                f"\nТЕКСТ:\n{text}")

    def task_combined(text: str) -> str:
        return (f"{rules}\n\nТИПЫ СУЩНОСТЕЙ: {types_line}\n"
                f"Ответ — JSON: {{\"entities\":[{{\"name\":\"...\",\"type\":\"...\"}}],"
                f"\"relations\":[{{\"source\":\"...\",\"type\":\"...\",\"target\":\"...\"}}]}}\n\nТЕКСТ:\n{text}")

    def task_batch(items: list) -> str:
        parts = "\n\n".join(f"--- ФРАГМЕНТ {i} ---\n{it['text']}" for i, it in enumerate(items))
        return (f"{rules}\n\nТИПЫ СУЩНОСТЕЙ: {types_line}\n"
                f"Ответ — JSON: {{\"chunks\":[{{\"i\":0,\"entities\":[{{\"name\":\"...\",\"type\":\"...\"}}],"
                f"\"relations\":[{{\"source\":\"...\",\"type\":\"...\",\"target\":\"...\"}}]}}]}}\n"
                f"ВАЖНО: связи каждого фрагмента клади ТОЛЬКО в его объект, не смешивай фрагменты.\n\n{parts}")

    def parse(raw: str) -> dict:
        """Достать JSON из ответа модели: берём сбалансированный объект, а не до последней скобки.

        Жадный разбор ломался там, где модель добавляла пояснения после JSON (режим «пачка»:
        ответ разложен по фрагментам и после него шёл текст) — результат приходил пустым.
        """
        s = raw or ""
        for start in (m.start() for m in re.finditer(r"\{", s)):
            depth = 0
            for i in range(start, len(s)):
                if s[i] == "{":
                    depth += 1
                elif s[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(s[start:i + 1])
                            if isinstance(obj, dict):
                                return obj
                        except Exception:
                            pass
                        break
        return {}

    async def call(prompt: str, tag: str) -> dict:
        t0 = time.time()
        raw = await entity_extractor._call_llm_once(
            prompt=prompt, model=model, llm_url=url, chunk_id=tag, pass_name=tag,
            api_key=key, provider=provider)
        dt = time.time() - t0
        text = raw.get("raw") if isinstance(raw, dict) else str(raw)
        if not text:
            text = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else ""
        return {"data": parse(text), "sec": dt}

    def score(entities: list, relations: list, texts: list) -> dict:
        precise = related = rejected = 0
        types = Counter()
        for r in relations or []:
            s = str(r.get("source") or "").strip()
            t_ = str(r.get("target") or "").strip()
            code = normalize_relation(r.get("type") or "")
            st = next((str(e.get("type")) for e in (entities or []) if str(e.get("name")) == s), "")
            tt = next((str(e.get("type")) for e in (entities or []) if str(e.get("name")) == t_), "")
            ok, _ = validate_triple(st, code, tt)
            if not ok:
                rejected += 1
                continue
            types[code] += 1
            if code == "related-to":
                related += 1
            else:
                precise += 1
        return {"сущностей": len(entities or []), "связей": len(relations or []),
                "точных": precise, "связано_с": related, "отброшено_словарём": rejected,
                "типы": dict(types.most_common(8))}

    results = {}

    # ── Режим A: два вызова на фрагмент ──
    log("\n=== режим A: два вызова на фрагмент ===")
    calls = 0
    t0 = time.time()
    ent_all = rel_all = 0
    acc = {"сущностей": 0, "связей": 0, "точных": 0, "связано_с": 0, "отброшено_словарём": 0, "типы": Counter()}
    for ch in chunks:
        r1 = await call(task_entities(ch["text"]), "ent"); calls += 1
        ents = r1["data"].get("entities") or []
        r2 = await call(task_relations(ch["text"], ents), "rel"); calls += 1
        rels = r2["data"].get("relations") or []
        s = score(ents, rels, [ch["text"]])
        for k in ("сущностей", "связей", "точных", "связано_с", "отброшено_словарём"):
            acc[k] += s[k]
        acc["типы"].update(s["типы"])
    results["A_два_вызова"] = {**{k: acc[k] for k in acc if k != "типы"},
                              "типы": dict(acc["типы"].most_common(8)),
                              "вызовов": calls, "секунд": round(time.time() - t0, 1)}

    # ── Режим B: один вызов на фрагмент ──
    log("=== режим B: один совмещённый вызов на фрагмент ===")
    calls = 0
    t0 = time.time()
    acc = {"сущностей": 0, "связей": 0, "точных": 0, "связано_с": 0, "отброшено_словарём": 0, "типы": Counter()}
    for ch in chunks:
        r1 = await call(task_combined(ch["text"]), "comb"); calls += 1
        ents = r1["data"].get("entities") or []
        rels = r1["data"].get("relations") or []
        s = score(ents, rels, [ch["text"]])
        for k in ("сущностей", "связей", "точных", "связано_с", "отброшено_словарём"):
            acc[k] += s[k]
        acc["типы"].update(s["типы"])
    results["B_один_вызов"] = {**{k: acc[k] for k in acc if k != "типы"},
                              "типы": dict(acc["типы"].most_common(8)),
                              "вызовов": calls, "секунд": round(time.time() - t0, 1)}

    # ── Режим C: пачка фрагментов в одном вызове ──
    log(f"=== режим C: пачка по {args.batch} фрагмента в вызове ===")
    calls = 0
    t0 = time.time()
    misplaced = 0
    acc = {"сущностей": 0, "связей": 0, "точных": 0, "связано_с": 0, "отброшено_словарём": 0, "типы": Counter()}
    for i in range(0, len(chunks), args.batch):
        pack = chunks[i:i + args.batch]
        r1 = await call(task_batch(pack), f"pack{i}"); calls += 1
        for item in (r1["data"].get("chunks") or []):
            try:
                idx = int(item.get("i"))
            except Exception:
                continue
            if not (0 <= idx < len(pack)):
                continue
            ents = item.get("entities") or []
            rels = item.get("relations") or []
            s = score(ents, rels, [pack[idx]["text"]])
            for k in ("сущностей", "связей", "точных", "связано_с", "отброшено_словарём"):
                acc[k] += s[k]
            acc["типы"].update(s["типы"])
            # Привязка: имена связи должны быть в тексте ЭТОГО фрагмента
            low = pack[idx]["text"].lower()
            for rr in rels:
                for nm in (rr.get("source"), rr.get("target")):
                    nm = str(nm or "").strip().lower()
                    if nm and len(nm) > 3 and nm not in low:
                        misplaced += 1
                        break
    results["C_пачка"] = {**{k: acc[k] for k in acc if k != "типы"},
                         "типы": dict(acc["типы"].most_common(8)),
                         "вызовов": calls, "секунд": round(time.time() - t0, 1),
                         "связи_с_чужими_именами": misplaced}

    log("\n=== ИТОГ ===")
    for name, r in results.items():
        log(f"{name}: {json.dumps(r, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
