"""Поиск рабочих адресов для мёртвых источников + разбор отказов (404, SSL, пустой селектор).

Итог перепроверки источников: часть отдаёт 404 (адрес устарел), один не проходит проверку сертификата,
один отвечает 200, но ссылок по селектору нет, ещё несколько не отвечают вовсе. Гадать про «правильный
адрес» нельзя — прибор перебирает кандидатов и печатает код, тип содержимого и число ссылок, чтобы
замена в коде опиралась на факт.

Отдельно печатается причина SSL-отказа (издатель и subject сертификата) — от этого зависит лечение:
добавить корневой сертификат в образ или (нежелательно) отключить проверку для одного источника.

Запуск внутри контейнера api:  docker exec kag-api python /app/data/sources_fix_probe.py
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; KAG/0.1; +https://qd.gostsecret.ru)"
PAUSE_S = 2.0

CANDIDATES = {
    "ЦБ РФ RSS (нормативные акты)": [
        "https://www.cbr.ru/rss/",
        "https://www.cbr.ru/rss/RssNews",
        "https://cbr.ru/rss/RssNews",
        "https://www.cbr.ru/rss/RssPress",
        "https://www.cbr.ru/scripts/RssNews.asp",
    ],
    "ФНС RSS (письма)": [
        "https://www.nalog.gov.ru/rss/",
        "https://www.nalog.gov.ru/rn77/about_fts/about_nalog/rss/",
        "https://www.nalog.gov.ru/rn77/rss/",
    ],
    "ФСТЭК (документы)": [
        "https://fstec.ru/dokumenty",
        "http://fstec.ru/dokumenty",
        "https://fstec.ru/dokumenty/zakonodatelnye-i-podzakonnye-pravovye-akty",
    ],
    "ФСБ (НПА)": [
        "http://www.fsb.ru/fsb/npd.htm",
        "http://www.fsb.ru/fsb/npd/order.htm",
    ],
}


def fetch(url: str, insecure: bool = False, timeout: int = 20):
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read()
            ctype = r.headers.get("Content-Type", "")
            return r.status, body, ctype, time.time() - t0, ""
    except urllib.error.HTTPError as e:
        return e.code, b"", "", time.time() - t0, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return 0, b"", "", time.time() - t0, f"{type(e).__name__}: {str(e)[:90]}"


def main() -> int:
    results = {}
    for group, urls in CANDIDATES.items():
        print(f"\n=== {group} ===")
        results[group] = []
        for url in urls:
            code, body, ctype, seconds, err = fetch(url)
            text = body.decode("utf-8", "replace")
            is_rss = "<rss" in text[:2000].lower() or "<feed" in text[:2000].lower()
            items = text.lower().count("<item>") + text.lower().count("<entry")
            hrefs = text.count("href=")
            print(f"  {'OK  ' if code == 200 else 'ФЕЙЛ'} {url[:58]:<60} код {code:<4} "
                  f"{len(body):>7} б {seconds:5.2f} с {ctype[:28]}")
            if err:
                print(f"        {err}")
            if code == 200:
                print(f"        rss={is_rss} записей={items} ссылок={hrefs}")
            if code == 200 and not is_rss and hrefs:
                import re
                sample = re.findall(r'href=[\"\']([^\"\']+)[\"\']', text)
                docs = [s for s in sample if s.lower().endswith((".pdf", ".doc", ".docx", ".rtf"))]
                print(f"        прямых файлов на странице: {len(docs)}"
                      + (f"; пример {docs[0][:80]}" if docs else ""))
            results[group].append({"url": url, "code": code, "bytes": len(body),
                                   "rss": is_rss, "items": items, "hrefs": hrefs, "error": err})
            time.sleep(PAUSE_S)

    print("\n=== ПРИЧИНА SSL-ОТКАЗА (fstec.ru) ===")
    try:
        import socket

        host = "fstec.ru"
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
                print(f"  проверка прошла: {cert.get('subject')}")
    except Exception as e:  # noqa: BLE001
        print(f"  проверка не прошла: {type(e).__name__}: {str(e)[:160]}")
        try:
            ctx = ssl._create_unverified_context()
            with socket.create_connection((host, 443), timeout=15) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    cert = ss.getpeercert()
                    print(f"  без проверки: subject={cert.get('subject')}")
                    print(f"  издатель: {cert.get('issuer')}")
        except Exception as e2:  # noqa: BLE001
            print(f"  даже без проверки не вышло: {type(e2).__name__}: {str(e2)[:120]}")

    with open("/tmp/sources_fix.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("\nрезультаты: /tmp/sources_fix.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
