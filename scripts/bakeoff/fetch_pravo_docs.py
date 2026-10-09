"""Загрузка тестовой подборки официальных российских документов с портала правовой информации.

Зачем: нужен реальный материал для проверки словаря видов документов и типов связей. Официальные
документы государственных органов по ГК РФ ст. 1259 п. 6 не являются объектами авторского права,
поэтому лицензионных вопросов нет (в отличие от датасетов с лицензией CC BY-NC).

Источник: Официальный интернет-портал правовой информации (publication.pravo.gov.ru).
API: /api/Documents?pageSize=100&index=N — список; /file/pdf?eoNumber=... — сам документ.

Запуск: python scripts/bakeoff/fetch_pravo_docs.py <куда_положить> [сколько]
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "http://publication.pravo.gov.ru/api/Documents"
PDF = "http://publication.pravo.gov.ru/file/pdf"
# виды, которые нам интересны в первую очередь (по названию документа)
WANTED = ["Федеральный закон", "Постановление", "Приказ", "Распоряжение", "Указ",
          "Соглашение", "Правила", "Положение", "Концепция", "Доктрина", "Стратегия"]


def _get(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "kag-testdata/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def collect(pages: int = 3) -> list[dict]:
    items: list[dict] = []
    for page in range(1, pages + 1):
        data = json.loads(_get(f"{API}?pageSize=100&index={page}").decode("utf-8"))
        items.extend(data.get("items") or [])
        print(f"  страница {page}: всего в ответе {len(data.get('items') or [])}, накоплено {len(items)}")
    return items


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "pravo_docs")
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    out.mkdir(parents=True, exist_ok=True)

    print("=== список документов ===")
    items = collect()
    picked: list[dict] = []
    seen_kinds: dict[str, int] = {}
    for it in items:
        title = (it.get("title") or "").strip()
        kind = next((w for w in WANTED if title.lower().startswith(w.lower())), None)
        if not kind:
            continue
        # не больше 5 документов одного вида — нужна пестрота
        if seen_kinds.get(kind, 0) >= 5:
            continue
        if not it.get("eoNumber") or not (it.get("pdfFileLength") or 0):
            continue
        seen_kinds[kind] = seen_kinds.get(kind, 0) + 1
        picked.append({**it, "_kind": kind})
        if len(picked) >= want:
            break

    print(f"\n=== качаю {len(picked)} документов ===")
    rows = []
    for it in picked:
        eo = it["eoNumber"]
        name = re.sub(r"[^\w\s\-.()]", "", (it.get("title") or eo))[:90].strip().replace(" ", "_")
        path = out / f"{eo}_{name}.pdf"
        if path.exists() and path.stat().st_size > 1000:
            rows.append((it["_kind"], eo, path.stat().st_size, path.name, True))
            continue
        try:
            blob = _get(f"{PDF}?eoNumber={urllib.parse.quote(eo)}", timeout=180)
            path.write_bytes(blob)
            rows.append((it["_kind"], eo, len(blob), path.name, True))
            print(f"  ok  {it['_kind']:<20} {len(blob)/1024/1024:5.2f} МБ  {path.name[:70]}")
        except Exception as e:  # noqa: BLE001
            rows.append((it["_kind"], eo, 0, path.name, False))
            print(f"  ФЕЙЛ {it['_kind']:<20} {str(e)[:60]}")
        time.sleep(0.4)

    ok = [r for r in rows if r[4]]
    print(f"\nскачано: {len(ok)} из {len(rows)}; суммарно {sum(r[2] for r in ok)/1024/1024:.1f} МБ")
    kinds: dict[str, int] = {}
    for r in ok:
        kinds[r[0]] = kinds.get(r[0], 0) + 1
    print("по видам:", json.dumps(kinds, ensure_ascii=False))
    (out / "_index.json").write_text(json.dumps(
        [{"kind": r[0], "eoNumber": r[1], "bytes": r[2], "file": r[3]} for r in ok],
        ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
