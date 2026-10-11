"""Диагностика: что именно модель кладёт в evidence и есть ли это в тексте.

Отправляет фрагмент напрямую модели с ЖЁСТКИМ требованием скопировать цитату символа-в-символ
и печатает: цитату, проверку по трём первым значимым словам и первые знаки самого текста.
Ответ на вопрос: модель пересказывает или копирует, и совпадает ли копия с текстом.

Запуск внутри контейнера:
    docker exec kag-api python /tmp/probe_citation_check.py <документ> [номер_фрагмента]
"""

import asyncio
import json
import re
import sys

import requests

DOC = sys.argv[1] if len(sys.argv) > 1 else "26842063-8042-4600-813f-0b19de045f44"
WANT = sys.argv[2] if len(sys.argv) > 2 else "00001"

from src.api.services.provider_service import provider_service  # noqa: E402

cfg = provider_service.get_function_llm_config("graph")
pr = cfg.get("parameters") or {}

from src.indexing.embeddings_service import service_for_document  # noqa: E402

chunks = asyncio.run(service_for_document(DOC).get_document_chunks(DOC))
ch = [c for c in chunks if str(c.get("chunk_id") or "").endswith(WANT)]
if not ch:
    print("фрагмент не найден:", WANT)
    raise SystemExit(2)
text = ch[0].get("content") or ch[0].get("text") or ""
print("фрагмент:", str(ch[0].get("chunk_id"))[-12:], "| знаков:", len(text))

prompt = (
    "Извлеки связи между сущностями. Только JSON, без пояснений.\n\n"
    'Формат: {"relations":[{"source":"...","target":"...","type":"тип","evidence":"цитата"}]}\n\n'
    "ВАЖНО про evidence: это ДОСЛОВНАЯ копия части текста ниже — символ в символ, включая тире\n"
    "и кавычки. Не пересказывай и не исправляй. Если скопировать не получается — evidence: null.\n\n"
    "Текст:\n---\n" + text[:3000] + "\n---\n\nJSON:"
)

url = (cfg.get("url") or "").rstrip("/")
if not url.endswith("/chat/completions"):
    url = url + "/chat/completions"
payload = {
    "model": cfg.get("model"),
    "messages": [{"role": "user", "content": prompt}],
    "temperature": pr.get("temperature", 0.05),
    "max_tokens": 1200,
}
if pr.get("no_think", True):
    payload["thinking"] = {"type": "disabled"}
    payload["enable_thinking"] = False

r = requests.post(url, headers={"Content-Type": "application/json",
                                "Authorization": "Bearer " + (cfg.get("api_key") or "")},
                  json=payload, timeout=180)
content = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
start = content.find("{")
try:
    rels = (json.loads(content[start:content.rfind("}") + 1]) or {}).get("relations") or []
except Exception as e:  # noqa: BLE001
    print("разбор не удался:", type(e).__name__, "| знаков:", len(content))
    rels = []


def flat(x):
    return re.sub(r"[^0-9a-zа-яё]+", " ", (x or "").lower()).strip()


print("связей:", len(rels))
for x in rels[:3]:
    ev = str(x.get("evidence") or "")
    print("  тип:", x.get("type"), "|", str(x.get("source"))[:24], "->", str(x.get("target"))[:24])
    print("   цитата модели:", repr(ev[:90]))
    if ev:
        words = [w for w in re.findall(r"[0-9A-Za-zА-Яа-яЁё]{5,}", ev)][:3]
        found = [bool(re.search(re.escape(w), text)) for w in words]
        print("   слова цитаты в тексте:", list(zip(words, found)))
        print("   мягкая нормализация (цитата целиком):", flat(ev)[:60] in flat(text))
print("--- начало самого текста ---")
print(repr(text[:260]))
