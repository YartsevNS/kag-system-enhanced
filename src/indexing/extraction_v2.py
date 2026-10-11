"""Схема извлечения графа v2 — по образцу щупа (scripts/bakeoff/probe_chunks.py).

Зачем: замер 11.10.2026 — щуп на чистом документе даёт 81 процент связей с подтверждённой цитатой,
конвейер на том же документе 0. Причина не в модели, а в запросе и разборе. Здесь собрано ровно то,
что доказано замером, и ничего лишнего:

  * системная часть минимальна (модель отвечает по образцу из запроса пользователя);
  * фрагмент уходит ЦЕЛИКОМ (медиана 1572 знака, максимум 1725 — обрезка до 1000 крала треть текста);
  * в запросе есть ОБРАЗЕЦ ответа с полем подтверждения (без него модель цитату не отдаёт);
  * в том же ответе приходят сущности с типами (нужны для проверки пар и записи в граф);
  * правила мягкие: «не подобрать цитату — null, но связь выведи» (запрет вывода даёт пустой ответ);
  * разбор терпимый: снимается markdown-обёртка, берётся от первой скобки до последней;
  * проверка цитаты — по нормализованному тексту (только буквы и цифры) и применяется всегда.

Схема включается настройкой system/graph_quality.extraction_scheme = "v2"; по умолчанию работает
прежняя. Вызов — одна функция: extract_chunk_v2(chunk_text, chunk_id).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = "Ты — эксперт по извлечению структурированных данных. Отвечай строго в JSON."


def _flat(x: str) -> str:
    """Нормализация под распознанный текст: сравниваем только буквы и цифры."""
    return re.sub(r"[^0-9a-zа-яё]+", " ", (x or "").lower()).strip()


def _relations_rules(rel_desc: str, ent_desc: str) -> str:
    return f"""Извлеки сущности и связи между ними. Только JSON, без пояснений.

Типы сущностей (type — только из этого списка):
{ent_desc}

Типы связей (type — только из этого списка):
{rel_desc}

Пример верного ответа (так выглядит каждая сущность и каждая связь):
{{"entities":[{{"name":"ГОСТ Р 57580.1—2017","type":"standard"}},{{"name":"Росстандарт","type":"organization"}}],
 "relations":[{{"source":"ГОСТ Р 57580.1—2017","target":"ГОСТ Р 57580.1","type":"replaces","evidence":"вводится взамен ГОСТ Р 57580.1-2017"}}]}}

Шаг 1. Найди в тексте сущности, укажи тип каждой.
Шаг 2. Найди короткие дословные фразы, из которых видна связь между двумя сущностями.
Шаг 3. Для каждой такой фразы укажи тип связи из списка и имена source/target ровно как в тексте.

Текст:
---
{{CHUNK}}
---

JSON:
{{"entities":[{{"name":"точное имя из текста","type":"тип сущности"}}],
 "relations":[{{"source":"...","target":"...","type":"тип связи","evidence":"дословная цитата из текста"}}]}}

Правила:
- entities: только те сущности, между которыми есть связи в этом тексте (обычно 2-12)
- relations: выведи ВСЕ связи, которые явно есть в тексте; пустой список — только если связей нет совсем
- source и target: точные имена из текста, такие же, как в entities
- evidence — ДОСЛОВНАЯ часть текста (10-60 знаков), скопированная БЕЗ ИЗМЕНЕНИЙ.
  Если подобрать не удалось — evidence: null, связь всё равно выведи
- выведи столько связей, сколько подтверждено цитатами (обычно 3-10)"""


def _parse_relaxed(content: str) -> Optional[Dict[str, Any]]:
    """Терпимый разбор: снимаем markdown-обёртку, берём от первой скобки до последней."""
    if not content:
        return None
    body = content
    if "```" in body:
        body = re.sub(r"^.*?```(?:json)?", "", body, flags=re.S).split("```")[0]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(body[start:end + 1])
    except Exception as e:  # noqa: BLE001
        logger.info(f"[graph][v2] разбор не удался: {type(e).__name__} | знаков {len(content)}")
        return None


def verify_relations(relations: List[Dict[str, Any]], chunk_text: str, chunk_id: str,
                     require: bool = False) -> List[Dict[str, Any]]:
    """Оставить только связи, чья дословная цитата найдена в тексте фрагмента.

    Работает всегда (счётчики в журнал), режет только при require=True. Мягкая нормализация —
    иначе на распознанном тексте тире и кавычки не совпадают с цитатой модели.
    """
    if not relations:
        return relations
    chunk = _flat(chunk_text)
    kept, no_ev, bad_ev = [], 0, 0
    for r in relations:
        ev = _flat(str(r.get("evidence") or ""))
        if not ev:
            no_ev += 1
        elif ev[:60] in chunk or ev in chunk:
            kept.append(r)
        else:
            bad_ev += 1
    if no_ev or bad_ev:
        logger.info(f"[graph][v2][цитата] {chunk_id}: связей {len(relations)}, без цитаты {no_ev}, "
                    f"цитата вне текста {bad_ev}, записано {len(kept)}")
    return kept if require else relations


def _clean_entities(raw: Any) -> List[Dict[str, str]]:
    """Сущности из ответа модели: имя как в тексте, тип — из закрытого списка.

    Тип из СТРОГОЙ ФОРМЫ записи перебивает ответ модели: «ГОСТ Р 57580.1—2017» → standard,
    «п. 5.2.1» → clause, «15.06.2026» → date — форма проверяема, а ответ модели нет. Выдуманный
    моделью тип не берём вовсе (сущность останется с пустым типом, и это видно в аудите).
    """
    try:
        from src.indexing.entity_typing import refine_type
        from src.indexing.graph_ontology import ENTITY_TYPES
    except Exception:  # noqa: BLE001
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        kind = str(e.get("type") or "").strip().lower()
        if kind not in ENTITY_TYPES:
            kind = ""
        out.append({"name": name, "type": refine_type(name, kind) or kind,
                    # Схема v2 не просит модель оценивать уверенность: связь подтверждается ЦИТАТОЙ
                    # из текста, а не самооценкой модели. Поле нужно только потому, что запись в
                    # граф его ожидает у каждой сущности.
                    "confidence": 1.0})
    return out


def extract_chunk_v2(chunk_text: str, chunk_id: str = "", require_evidence: bool = False,
                     diagnostics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Извлечь сущности и связи из фрагмента по схеме v2. Возвращает {"entities": [...],
    "relations": [...]}.

    Один вызов модели на фрагмент: типы сущностей и типы связей + текст целиком + образец ответа +
    мягкие правила. Сущности приходят в ТОМ ЖЕ ответе (второго прохода нет), и типы их концов
    нужны не для красоты: без типов связь нельзя ни проверить правилом пар (validate_triple),
    ни записать в граф. Текст не урезается.

    diagnostics — необязательный словарь, который заполняется тем, что прибору (и журналу) нужно
    видеть: СЫРОЙ ответ модели, длина запроса, время вызова, сколько связей вернула модель и
    сколько из них сохранила проверка. Нужен, чтобы аудит мерил ровно этот код, а не свою копию
    промпта (на расхождении копии и конвейера прошлый заход и потерял день).
    """
    out: Dict[str, Any] = {"entities": [], "relations": []}
    if diagnostics is not None:
        diagnostics.update({"chunk_id": chunk_id, "chunk_chars": len(chunk_text or ""), "raw": "",
                            "seconds": 0.0, "prompt_chars": 0, "status": None,
                            "entities_model": [], "relations_model": [], "relations_kept": [],
                            "usage": {}})
    if not chunk_text or len(chunk_text.strip()) < 20:
        if diagnostics is not None:
            diagnostics["status"] = "фрагмент пуст"
        return out
    try:
        from src.api.services.provider_service import provider_service

        cfg = provider_service.get_function_llm_config("graph")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph][v2] нет привязки модели: {e}")
        return out
    if not cfg or not cfg.get("model"):
        return out
    params = cfg.get("parameters") or {}

    try:
        from src.indexing.graph_ontology import entity_types_compact, relations_compact

        # Список типов связей — компактный и ЦЕЛИКОМ: по строке на тип, <код> — <смысл>, около 1,3
        # тысячи знаков. Раньше здесь стояла обрезка prompt_rules()[:3000]: полный текст правил —
        # 9943 знака прозы (примеры, разбор путаницы), и обрезка рубила список ПОСРЕДИНЕ описания
        # типа, так что часть типов модель не видела вовсе, а инструкция приходила искажённой.
        rel_desc = relations_compact()
        # Типы сущностей нужны здесь, а не отдельным проходом: без типов концов связь нельзя ни
        # проверить правилом пар (validate_triple), ни записать в граф. Замер 11.10.2026 по ГОСТ
        # 57580: 1305 связей из 1418 (92 %) были не проверяемы именно из-за отсутствия типов.
        ent_desc = entity_types_compact()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph][v2] списки типов не взяты: {e}")
        rel_desc, ent_desc = "", ""

    prompt = _relations_rules(rel_desc, ent_desc).replace("{CHUNK}", chunk_text)
    url = (cfg.get("url") or "").rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    payload = {
        "model": cfg.get("model"),
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
        "temperature": params.get("temperature", 0.05),
        "max_tokens": 1600,
    }
    if params.get("no_think", True):
        payload["thinking"] = {"type": "disabled"}
        payload["enable_thinking"] = False

    if diagnostics is not None:
        diagnostics["prompt_chars"] = len(prompt)

    t0 = time.perf_counter()
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph][v2] вызов не удался: {type(e).__name__} {str(e)[:120]}")
        if diagnostics is not None:
            diagnostics["seconds"] = round(time.perf_counter() - t0, 2)
            diagnostics["status"] = f"сеть: {type(e).__name__}"
        return out
    if diagnostics is not None:
        diagnostics["seconds"] = round(time.perf_counter() - t0, 2)
        diagnostics["status"] = r.status_code
    if r.status_code != 200:
        logger.warning(f"[graph][v2] код {r.status_code}: {r.text[:150]}")
        return out
    data = r.json()
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    parsed = _parse_relaxed(content) or {}
    entities = _clean_entities(parsed.get("entities"))
    relations = [x for x in (parsed.get("relations") or []) if x.get("source") and x.get("target")]
    out["entities"] = entities
    out["relations"] = verify_relations(relations, chunk_text, chunk_id, require=require_evidence)
    if diagnostics is not None:
        diagnostics["raw"] = content
        diagnostics["usage"] = data.get("usage") or {}
        diagnostics["entities_model"] = entities
        diagnostics["relations_model"] = relations
        diagnostics["relations_kept"] = out["relations"]
        diagnostics["разбор"] = "ок" if parsed else "не разобран"
    return out
