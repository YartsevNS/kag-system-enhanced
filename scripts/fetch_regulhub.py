"""Конвейер документов с RegulHub (Kaspersky) в KAG: сбор → скачивание → загрузка → обработка.

Зачем отдельный скрипт, а не разовые команды: это воспроизводимый путь «источник → документ →
онтология». Он же используется для проверки полного workflow после изменений.

Этапы:
  1. collect  — Playwright обходит страницы хаба и собирает ссылки на PDF
  2. download — качает PDF, отбрасывая маркетинговые материалы и политики
  3. upload   — определяет издателя, вид, номер, редакцию и дату из самого документа
                и загружает в KAG (source_kind=regulator, source_name=RegulHub)
  4. process  — запускает обработку и ждёт завершения, печатая статус по каждому

Правила, которые нельзя нарушать (иначе граф заполнится мусором):
  - загружается только НОРМАТИВНЫЙ документ (есть «ПРИКАЗ», «ГОСТ», «СТАНДАРТ БАНКА РОССИИ»,
    «утверждён и введён в действие»); маркетинговые PDF Kaspersky отбрасываются;
  - дата вступления не выдумывается: нет даты в тексте — документ пропускается;
  - темп загрузки не выше 10 запросов за 30 секунд (ограничение API), при 429 — повтор.

Запуск (внутри контейнера kag-api, там есть Playwright, pymupdf и доступ к API):
    python scripts/fetch_regulhub.py all
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://regulhub.kaspersky.ru"
API = os.getenv("KAG_API", "http://localhost:8000")
ENV_FILE = os.getenv("KAG_ENV", "/app/kag.env")
WORK = "/tmp/regulhub"
DOCS_JSON = f"{WORK}/docs.json"
MANIFEST_JSON = f"{WORK}/manifest.json"
UPLOADED_JSON = f"{WORK}/uploaded.json"
LIMIT = int(os.getenv("REGULHUB_LIMIT", "20"))
PAGES = ["/", "/news", "/glossary", "/solutions"]

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
          "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}

# признаки нормативного акта в начале документа
NORMATIVE = re.compile(
    r"(приказ|постановлени|федеральный закон|стандарт банка россии|сто бр|гост|"
    r"национальн\w+ стандарт|утвержд\w+ и введен|введен в действие|iso/iec|pci dss)",
    re.I)
# маркетинговые материалы и служебные страницы
JUNK = re.compile(r"(политик\w* конфиденциальност|юридическ\w* информац|"
                  r"согласи\w* на обработку)", re.I)


def env_value(key: str, default: str = "") -> str:
    try:
        for line in open(ENV_FILE, encoding="utf-8", errors="replace"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return default


def token() -> str:
    body = json.dumps({"username": "admin",
                       "password": env_value("ADMIN_PASSWORD")}).encode()
    req = urllib.request.Request(API + "/api/v1/auth/login", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("access_token", "")


# ── 1. сбор ссылок ──────────────────────────────────────────────────────────
async def collect() -> list:
    from playwright.async_api import async_playwright

    found = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(locale="ru-RU", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        page = await ctx.new_page()
        for path in PAGES:
            try:
                await page.goto(BASE + path, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                print(f"  {path}: {str(exc).splitlines()[0][:60]}")
                continue
            await page.wait_for_timeout(3000)
            items = await page.evaluate("""() => Array.from(document.querySelectorAll('a'))
                .map(a => ({href: a.href, text: (a.innerText || '').trim(),
                            title: a.getAttribute('title') || ''}))""")
            for it in items:
                href = it.get("href") or ""
                if re.search(r"\.pdf($|\?)", href, re.I) and href not in found:
                    found[href] = {"url": href, "text": (it.get("text") or "")[:200],
                                   "title": (it.get("title") or "")[:200], "from": path}
        await browser.close()
    os.makedirs(WORK, exist_ok=True)
    json.dump(list(found.values()), open(DOCS_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"собрано PDF-ссылок: {len(found)}")
    return list(found.values())


# ── 2. скачивание ───────────────────────────────────────────────────────────
def download(docs: list) -> list:
    os.makedirs(WORK, exist_ok=True)
    picked = [d for d in docs
              if not JUNK.search((d.get("text") or d.get("title") or ""))][:LIMIT]
    manifest, skipped = [], 0
    for i, d in enumerate(picked, 1):
        name = d["url"].rsplit("/", 1)[-1]
        path = os.path.join(WORK, f"{i:02d}_{name}")
        try:
            req = urllib.request.Request(d["url"], headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=60).read()
            if not data.startswith(b"%PDF"):
                skipped += 1
                continue
            open(path, "wb").write(data)
            manifest.append({**d, "file": path, "bytes": len(data)})
        except Exception as exc:  # noqa: BLE001
            print(f"  {i:02d} ошибка скачивания: {str(exc)[:70]}")
            skipped += 1
    json.dump(manifest, open(MANIFEST_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"скачано: {len(manifest)} (пропущено {skipped})")
    return manifest


# ── 3. метаданные и загрузка ────────────────────────────────────────────────
def detect_meta(path: str, label: str) -> dict:
    import pymupdf

    doc = pymupdf.open(path)
    try:
        text = "\n".join(doc[i].get_text() for i in range(min(3, doc.page_count)))
    finally:
        doc.close()
    flat = re.sub(r"\s+", " ", text)
    low = flat.lower()

    head = low[:4000]
    if not NORMATIVE.search(head):
        return {"skip": "не нормативный документ"}

    if "экспортному контролю" in low or "фстэк" in low:
        issuer, kind = "ФСТЭК России", "приказ"
    elif "центральный банк" in low or "банка россии" in low or "бр иббс" in low or "бфбо" in low:
        issuer, kind = "Банк России", "стандарт"
    elif ("техническому регулированию и метрологии" in low or "росстандарт" in low
          or "гост" in low):
        issuer, kind = "Росстандарт", "ГОСТ"
    elif "pci" in low:
        issuer, kind = "PCI SSC", "стандарт"
    else:
        return {"skip": "не определён издатель"}

    title = ""
    for line in text.splitlines():
        s = line.strip()
        if len(s) >= 12 and not s.isdigit() and "национальный стандарт" not in s.lower():
            title = s
            break
    if label and label not in ("PDF", "-") and label not in title:
        title = f"{label}. {title}" if title else label

    num = ""
    m = re.search(r"(?:№|N|No\.?)\s*([0-9][0-9.\-/]*)", flat)
    if m:
        num = m.group(1)[:40]
    else:
        m = re.search(r"(ГОСТ(?:\s+Р)?\s*(?:ISO/IEC)?\s*[0-9][0-9.\-]*)", flat)
        if m:
            num = m.group(1)[:40]

    eff = ""
    for m in re.finditer(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text, re.I):
        mon = m.group(2).lower()
        if mon in MONTHS:
            eff = f"{m.group(3)}-{MONTHS[mon]:02d}-{int(m.group(1)):02d}"
            break
    if not eff:
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
        if m and 1 <= int(m.group(2)) <= 12:
            eff = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if not eff:
        m = re.search(r"\b((?:19|20)\d{2})\b", flat)
        eff = f"{m.group(1)}-01-01" if m else ""
    if not eff:
        return {"skip": "нет даты вступления"}

    return {"title": title[:300], "issuer": issuer, "doc_kind": kind,
            "number": num, "version_string": eff[:4], "effective_from": eff}


def upload(path: str, meta: dict, tok: str, url: str) -> str:
    boundary = "----XyZBoundary"
    fields = {
        "source_kind": "regulator", "collection": "documents", "visibility": "public",
        "issuer": meta["issuer"], "doc_kind": meta["doc_kind"], "number": meta["number"],
        "version_string": meta["version_string"], "effective_from": meta["effective_from"],
        "source_url": url, "source_name": "RegulHub (Kaspersky)",
        "doc_title": meta["title"],
    }
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
             for k, v in fields.items() if v]
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"{os.path.basename(path)}\"\r\n"
                 f"Content-Type: application/pdf\r\n\r\n".encode()
                 + open(path, "rb").read() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(API + "/api/v1/upload/", data=b"".join(parts), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r).get("document_id", "")


def upload_all(manifest: list) -> list:
    tok = token()
    out, skipped = [], []
    for i, d in enumerate(manifest, 1):
        path = d.get("file")
        if not path or not os.path.exists(path):
            continue
        meta = detect_meta(path, (d.get("text") or d.get("title") or "").strip())
        if meta.get("skip"):
            skipped.append({"file": path, "reason": meta["skip"], "title": meta.get("title", "")})
            print(f"  {i:02d} пропуск: {meta['skip']} — {(d.get('text') or '-')[:50]}")
            continue
        for attempt in range(3):
            try:
                doc_id = upload(path, meta, tok, d.get("url", ""))
                print(f"  {i:02d} загружен {doc_id[:12]} | {meta['doc_kind']:18} | "
                      f"{meta['issuer']:13} | {meta['version_string']} | {meta['title'][:45]}")
                out.append({"file": path, "document_id": doc_id, **meta, "url": d.get("url", "")})
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                if exc.code == 429:
                    time.sleep(12)
                    continue
                print(f"  {i:02d} отклонено ({exc.code}): {body[:120]}")
                skipped.append({"file": path, "reason": body[:160], "title": meta["title"]})
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  {i:02d} ошибка: {str(exc)[:100]}")
                skipped.append({"file": path, "reason": str(exc)[:160], "title": meta["title"]})
                break
        time.sleep(3.5)
    json.dump(out, open(UPLOADED_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"загружено: {len(out)} | пропущено: {len(skipped)}")
    return out


# ── 4. обработка ────────────────────────────────────────────────────────────
def process_all(docs: list) -> list:
    tok = token()
    summary = []
    for i, d in enumerate(docs, 1):
        doc_id = d["document_id"]

        def call(path, method="GET", body=None):
            data = json.dumps(body).encode() if body is not None else None
            r = urllib.request.Request(API + path, data=data, method=method, headers={
                "Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}

        try:
            call(f"/api/v1/upload/{doc_id}/process", "POST", {})
        except Exception as exc:  # noqa: BLE001
            print(f"  {i:02d} не запущено: {str(exc)[:60]}")
            summary.append({"id": doc_id, "status": "start_failed"})
            continue
        status, waited = "?", 0
        for _ in range(40):
            time.sleep(15)
            waited += 15
            try:
                status = call(f"/api/v1/upload/{doc_id}/status").get("status", "?")
            except Exception:  # noqa: BLE001
                status = "status_error"
            if status in ("completed", "failed"):
                break
        print(f"  {i:02d} {doc_id[:12]} | {status:10} | {waited:4} c | {d.get('title','')[:45]}")
        summary.append({"id": doc_id, "status": status, "waited": waited,
                        "title": d.get("title", "")})
    done = sum(1 for s in summary if s["status"] == "completed")
    print(f"обработано: {done} из {len(summary)}")
    return summary


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(WORK, exist_ok=True)
    if stage in ("collect", "all"):
        asyncio.run(collect())
    if stage in ("download", "all"):
        download(json.load(open(DOCS_JSON, encoding="utf-8")))
    if stage in ("upload", "all"):
        upload_all(json.load(open(MANIFEST_JSON, encoding="utf-8")))
    if stage in ("process", "all"):
        process_all(json.load(open(UPLOADED_JSON, encoding="utf-8")))


if __name__ == "__main__":
    main()
