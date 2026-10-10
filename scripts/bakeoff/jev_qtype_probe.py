"""Проверка: какие типы вопросов принимает JEV — нужен мультитемпоральный (несколько тем с вероятностью).

Задача: тема документа у нас МНОГОЗНАЧНАЯ (документ может быть про ИБ и про право одновременно), а
одиночный выбор этого не выражает. Пробуем три формы и смотрим, что API принимает и что возвращает:
  1) choice        — как сейчас (один вариант из списка);
  2) probability   — вероятности по всем критериям сразу;
  3) boolean       — отдельный вопрос на каждую тему (из них складывается мультизначность).
Запуск: python scripts/bakeoff/jev_qtype_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
URL = "https://polza.ai/api/v1/systemone"
MODEL = "typesafe/jev"

SAMPLE = ("ГОСТ Р 57580.1-2017. Безопасность финансовых (банковских) операций. Защита информации "
          "финансовых организаций. Требования к обеспечению информационной безопасности.")


def ask(key: str, questions: dict, state: str = SAMPLE, timeout: int = 120) -> dict:
    body = json.dumps({"model": MODEL, "state": state, "questions": questions},
                      ensure_ascii=False).encode()
    req = urllib.request.Request(
        URL, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    key = os.environ.get("POLZA_API_KEY") or os.environ.get("HERMES_CUSTOM_POLZA_API_KEY")
    if not key:
        print("нет ключа")
        return 2
    from src.indexing import document_topics as dt

    titles = {c: m.get("title", c) for c, m in dt.RUBRICS.items()}
    variants = {
        "choice": {"тема": {"type": "choice", "instructions": "О чём документ?", "criteria": titles}},
        "probability": {"темы": {"type": "probability",
                                 "instructions": "Какова вероятность, что документ относится к теме?",
                                 "criteria": titles}},
        "multichoice": {"темы": {"type": "multichoice",
                                 "instructions": "Выбери все подходящие темы (может быть несколько).",
                                 "criteria": titles}},
        "boolean_x2": {
            "тема_иБ": {"type": "boolean", "instructions": "Документ про информационную безопасность?"},
            "тема_право": {"type": "boolean", "instructions": "Документ про юриспруденцию?"},
        },
    }
    for name, qs in variants.items():
        try:
            data = ask(key, qs)
            ans = data.get("answers") or {}
            print(f"\n=== {name}: принят ===")
            print(json.dumps(ans, ensure_ascii=False)[:600])
            print("usage:", json.dumps(data.get("usage") or {}, ensure_ascii=False)[:200])
        except urllib.error.HTTPError as e:
            print(f"\n=== {name}: ОТКЛОНЁН HTTP {e.code} ===")
            print(e.read().decode("utf-8", "replace")[:300])
        except Exception as e:  # noqa: BLE001
            print(f"\n=== {name}: ошибка {type(e).__name__}: {str(e)[:200]} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
