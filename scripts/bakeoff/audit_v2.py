"""Аудит извлечения связей по схеме v2 — один прибор на три уровня.

    python /tmp/audit/audit_v2.py chunk <документ>          # ОДИН фрагмент: что модель вернула, дословно
    python /tmp/audit/audit_v2.py set   <документ> 10       # набор фрагментов, равномерно по документу
    python /tmp/audit/audit_v2.py doc   <документ>          # ВСЕ фрагменты документа

Зачем свой прибор, а не прежние щупы: в probe_chunks.py и probe_evidence.py промпт СКОПИРОВАН внутрь
скрипта, поэтому щуп мерил не то, что делал конвейер (отсюда «щуп 81 процент, конвейер 0»). Здесь
вызывается ровно рабочая функция extract_chunk_v2 с диагностикой — что измерено, то и поедет.

Метрики одинаковые на всех уровнях:
  ТОЧНОСТЬ        — доля связей, чья дословная цитата (evidence) найдена в тексте фрагмента;
  выход модели    — связей вернула / из них без цитаты / ответов не разобрали;
  схема           — сколько связей проходит проверку пар validate_triple и почему прочие нет;
  типы            — сколько связей каждого типа и какова у него доля подтверждённых;
  цена            — секунды на фрагмент, знаков в запросе, токены (в т.ч. взятые из кэша).

Сырые ответы модели сохраняются в JSON рядом с отчётом — чтобы смотреть глазами, а не верить цифре.

Запуск без выката (правленый код лежит в копии src, рабочие контейнеры не тронуты):
  см. scripts/bakeoff/run_audit_v2.sh
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import Counter, defaultdict

DOC = sys.argv[2] if len(sys.argv) > 2 else "2873e687-d476-43e6-b94e-f4dd14eaead8"
LEVEL = (sys.argv[1] if len(sys.argv) > 1 else "chunk").lower()
COUNT = int(sys.argv[3]) if len(sys.argv) > 3 else 10
OUT = sys.argv[4] if len(sys.argv) > 4 else ""

from src.indexing.embeddings_service import service_for_document  # noqa: E402
from src.indexing.entity_typing import refine_type  # noqa: E402
from src.indexing.extraction_v2 import extract_chunk_v2  # noqa: E402
from src.indexing.graph_ontology import (  # noqa: E402
    RELATION_BY_CODE,
    normalize_relation,
    relations_compact,
    validate_triple,
)


def flat(x: str) -> str:
    """Та же мягкая нормализация, что в проверке цитаты: только буквы и цифры."""
    return re.sub(r"[^0-9a-zа-яё]+", " ", (x or "").lower()).strip()


def pick(chunks: list[dict], level: str) -> list[dict]:
    if level == "chunk":
        # Первый содержательный фрагмент: служебные (титул, оглавление) для замера не годятся.
        for c in chunks:
            if len(str(c.get("content") or "")) > 300:
                return [c]
        return chunks[:1]
    if level == "set":
        step = max(1, len(chunks) // COUNT)
        return [chunks[i] for i in range(0, len(chunks), step)][:COUNT]
    return chunks


def main() -> int:
    t_all = time.perf_counter()
    chunks = asyncio.run(service_for_document(DOC).get_document_chunks(DOC))
    picked = pick(chunks, LEVEL)
    print("=" * 96)
    print(f"АУДИТ v2 | уровень «{LEVEL}» | документ {DOC}")
    print(f"фрагментов в документе: {len(chunks)} | к проверке: {len(picked)}")
    print(f"список типов в задании: {len(relations_compact())} знаков "
          f"(было: обрезка правил 3000 знаков)")
    print("=" * 96)

    total_rel = total_ev = total_found = bad_json = calls = 0
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"всего": 0, "подтв": 0})
    schema_ok = schema_bad = typed_bad = 0
    schema_reasons: Counter = Counter()
    tokens = Counter()
    seconds: list[float] = []
    raw_dump: list[dict] = []

    for i, ch in enumerate(picked, 1):
        cid = str(ch.get("chunk_id") or "")
        text = str(ch.get("content") or ch.get("text") or "")
        diag: dict = {}
        res = extract_chunk_v2(text, cid, require_evidence=False, diagnostics=diag)
        rels = diag.get("relations_model") or []
        calls += 1
        seconds.append(float(diag.get("seconds") or 0.0))
        usage = diag.get("usage") or {}
        tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
        tokens["cached"] += int(usage.get("prompt_cache_hit_tokens")
                                 or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        tokens["вывод"] += int(usage.get("completion_tokens") or 0)
        if diag.get("разбор") == "не разобран":
            bad_json += 1

        chunk_flat = flat(text)
        found_here = 0
        # Типы концов берём из ответа модели (entities в том же вызове), а если имя там не нашлось —
        # уточняем правилом формы записи. Так проверка пар видит то же, что поедет в граф.
        ent_types = {str(e.get("name") or ""): str(e.get("type") or "")
                     for e in (diag.get("entities_model") or [])}
        print(f"\n[{i}/{len(picked)}] фрагмент {cid[-8:]} | знаков {len(text)} | "
              f"запрос {diag.get('prompt_chars')} знаков | {diag.get('seconds')} с | "
              f"связей от модели {len(rels)} | сущностей {len(ent_types)}")
        if diag.get("status") != 200:
            print(f"    ВЫЗОВ НЕ УДАЛСЯ: {diag.get('status')}")

        for r in rels:
            total_rel += 1
            code = normalize_relation(str(r.get("type") or ""))
            ev = str(r.get("evidence") or "")
            ev_flat = flat(ev)
            hit = bool(ev_flat) and (ev_flat[:60] in chunk_flat or ev_flat in chunk_flat)
            total_ev += 1 if ev_flat else 0
            total_found += 1 if hit else 0
            found_here += 1 if hit else 0
            by_type[code]["всего"] += 1
            by_type[code]["подтв"] += 1 if hit else 0
            # Схема: типы концов — из ответа модели (entities), при промахе — из формы записи.
            src_name = str(r.get("source") or "")
            dst_name = str(r.get("target") or "")
            src_t = ent_types.get(src_name) or refine_type(src_name)
            dst_t = ent_types.get(dst_name) or refine_type(dst_name)
            ok, note = validate_triple(src_t, code, dst_t)
            if not src_t or not dst_t:
                typed_bad += 1
                schema_reasons["тип конца не определён формой записи"] += 1
            elif ok:
                schema_ok += 1
            else:
                schema_bad += 1
                schema_reasons[note[:70]] += 1
            if LEVEL == "chunk":
                mark = "цитата в тексте: ЕСТЬ" if hit else ("цитата пустая" if not ev_flat else "ЦИТАТЫ В ТЕКСТЕ НЕТ")
                print(f"    {code:24s} {str(r.get('source'))[:30]:30s} -> {str(r.get('target'))[:30]:30s}")
                print(f"        {mark} | {('«' + ev[:70] + '»') if ev else ''}")

        raw_dump.append({
            "chunk_id": cid, "chunk_chars": len(text), "seconds": diag.get("seconds"),
            "prompt_chars": diag.get("prompt_chars"), "status": diag.get("status"),
            "разбор": diag.get("разбор"), "usage": usage,
            # Текст фрагмента кладём в отчёт: иначе промах («цитаты в тексте нет») после прогона
            # не разобрать — непонятно, модель перефразировала цитату или придумала связь.
            "chunk_text": text,
            "entities": diag.get("entities_model") or [],
            "relations": rels, "raw": diag.get("raw"),
        })
        print(f"    у этого фрагмента подтверждено цитатой: {found_here} из {len(rels)}")

    # ── Итог ────────────────────────────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("ИТОГ")
    print("=" * 96)
    print(f"фрагментов проверено:               {calls}")
    print(f"связей вернула модель:              {total_rel}")
    print(f"  с непустой цитатой:               {total_ev}")
    print(f"  ЦИТАТА НАЙДЕНА В ТЕКСТЕ:          {total_found}   <-- ТОЧНОСТЬ ПОДТВЕРЖДЕНИЯ",
          f"{int(100 * total_found / total_rel) if total_rel else 0}%")
    print(f"ответов не разобрали:               {bad_json}")
    print(f"схема (пары тип-связь-тип): проходят {schema_ok} | нарушают {schema_bad} | "
          f"без типа конца {typed_bad}")
    if schema_reasons:
        print("  причины отказов схемы:")
        for reason, n in schema_reasons.most_common(8):
            print(f"    {n:4d}  {reason}")
    if seconds:
        print(f"время: сумма {sum(seconds):.1f} с | медиана {sorted(seconds)[len(seconds) // 2]:.1f} с | "
              f"среднее {sum(seconds) / len(seconds):.1f} с на фрагмент")
    print(f"токены: запрос {tokens['prompt']} | из кэша {tokens['cached']} | вывод {tokens['вывод']}")
    print(f"весь замер: {time.perf_counter() - t_all:.1f} с")
    if by_type:
        print("\nпо типам связей (всего / подтверждено):")
        for code, c in sorted(by_type.items(), key=lambda kv: -kv[1]["всего"]):
            share = int(100 * c["подтв"] / c["всего"]) if c["всего"] else 0
            title = getattr(RELATION_BY_CODE.get(code), "title", "")
            print(f"  {code:24s} {c['всего']:4d} / {c['подтв']:4d} ({share}%)  {title}")

    out = OUT or f"/tmp/audit_v2_{LEVEL}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "уровень": LEVEL, "документ": DOC, "фрагментов_в_документе": len(chunks),
            "проверено_фрагментов": calls,
            "связей": total_rel, "с_цитатой": total_ev, "цитата_в_тексте": total_found,
            "точность_проц": int(100 * total_found / total_rel) if total_rel else 0,
            "не_разобранных_ответов": bad_json,
            "схема": {"проходят": schema_ok, "нарушают": schema_bad, "без_типа_конца": typed_bad,
                      "причины": dict(schema_reasons)},
            "по_типам": {k: dict(v) for k, v in by_type.items()},
            "секунд_на_фрагмент": seconds,
            "токены": dict(tokens),
            "ответы_модели": raw_dump,
        }, f, ensure_ascii=False, indent=2)
    print(f"\nмашинный отчёт и СЫРЫЕ ответы модели: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
