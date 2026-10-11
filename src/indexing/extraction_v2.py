"""Схема извлечения графа v2 — по образцу щупа (scripts/bakeoff/probe_chunks.py).

Зачем: замер 11.10.2026 — щуп на чистом документе даёт 81 процент связей с подтверждённой цитатой,
конвейер на том же документе 0. Причина не в модели, а в запросе и разборе. Здесь собрано ровно то,
что доказано замером, и ничего лишнего:

  * системная часть минимальна (модель отвечает по образцу из запроса пользователя);
  * фрагмент уходит ЦЕЛИКОМ (медиана 1572 знака, максимум 1725 — обрезка до 1000 крала треть текста);
  * в запросе есть ОБРАЗЕЦ ответа с полем подтверждения (без него модель цитату не отдаёт);
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
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = "Ты — эксперт по извлечению структурированных данных. Отвечай строго в JSON."


def _flat(x: str) -> str:
    """Нормализация под распознанный текст: сравниваем только буквы и цифры."""
    return re.sub(r"[^0-9a-zа-яё]+", " ", (x or "").lower()).strip()


def _relations_rules(rel_desc: str) -> str:
    return f"""Извлеки связи между сущностями. Только JSON, без пояснений.

Типы связей (type — только из этого списка):
{rel_desc}

Пример верной пары (так выглядит каждая связь):
{{"source":"ГОСТ Р 57580.1—2017","target":"ГОСТ Р 57580.1","type":"replaces","evidence":"вводится взамен ГОСТ Р 57580.1-2017"}}

Шаг 1. Найди в тексте короткие дословные фразы, из которых видна связь между двумя сущностями.
Шаг 2. Для каждой такой фразы укажи тип связи из списка и имена source/target ровно как в тексте.

Текст:
---
{{CHUNK}}
---

JSON:
{{"relations":[{{"source":"...","target":"...","type":"тип связи","evidence":"дословная цитата из текста"}}]}}

Правила:
- выведи ВСЕ связи, которые явно есть в тексте; пустой список — только если связей нет совсем
- source и target: точные имена из текста
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


def extract_chunk_v2(chunk_text: str, chunk_id: str = "", require_evidence: bool = False) -> Dict[str, Any]:
    """Извлечь связи из фрагмента по схеме v2. Возвращает {"relations": [...]}.

    Один вызов модели на фрагмент: список типов + текст целиком + образец ответа + мягкие правила.
    Никаких сущностей отдельным проходом и никакого урезания текста.
    """
    out: Dict[str, Any] = {"relations": []}
    if not chunk_text or len(chunk_text.strip()) < 20:
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
        from src.indexing.graph_ontology import prompt_rules

        rel_desc = prompt_rules()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph][v2] правила онтологии не взяты: {e}")
        rel_desc = ""

    prompt = _relations_rules(rel_desc).replace("{CHUNK}", chunk_text)
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

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph][v2] вызов не удался: {type(e).__name__} {str(e)[:120]}")
        return out
    if r.status_code != 200:
        logger.warning(f"[graph][v2] код {r.status_code}: {r.text[:150]}")
        return out
    content = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    parsed = _parse_relaxed(content) or {}
    relations = [x for x in (parsed.get("relations") or []) if x.get("source") and x.get("target")]
    out["relations"] = verify_relations(relations, chunk_text, chunk_id, require=require_evidence)
    return out
