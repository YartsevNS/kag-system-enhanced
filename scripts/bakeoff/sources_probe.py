"""Перепроверка скачивания: живые страницы источников по информационной безопасности.

Зачем прибор. «Источник настроен» в коде не значит «по нему что-то скачается»: у сайтов меняются адреса
и разметка, и тогда скачивание молча даёт ноль файлов. Прибор ходит на страницы так же, как монитор
(с паузой между запросами, чтобы не выглядеть DDoS), и печатает по каждому источнику: код ответа,
размер страницы, сколько ссылок видит селектор и примеры. Ноль ссылок — дефект источника (адрес или
селектор), а не «нет новостей».

Пауза между запросами — 2 секунды (требование владельца) и ещё пауза перед перелистыванием страниц.

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/sources_probe.py [--all] [--save /app/data/sources_probe.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; KAG/0.1; +https://qd.gostsecret.ru)"
PAUSE_S = 2.0


def fetch(url: str, timeout: int = 30) -> tuple:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "ru-RU,ru;q=0.9"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return r.status, body, time.time() - t0, ""
    except urllib.error.HTTPError as e:
        return e.code, "", time.time() - t0, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return 0, "", time.time() - t0, f"{type(e).__name__}: {str(e)[:80]}"


def links(html: str, selector: str) -> list:
    """Ссылки по CSS-селектору источника. Пробуем настоящий парсер, иначе — разбор по шаблонам.

    Разбор по шаблонам нужен потому, что в контейнере может не быть bs4, а проверка источника не должна
    зависеть от наличия библиотеки: считаем по тем видам селекторов, которые реально используются в коде
    (`a[href$='.pdf']`, `a[href*='file/load']`, `a[href*='/Crosscut/LawActs/File/']`).
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        return [a.get("href") or "" for a in soup.select(selector)]
    except Exception:  # noqa: BLE001
        hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", html)
        found = []
        for sel in [s.strip() for s in selector.split(",")]:
            m = re.match(r"a\[href(\$?=|\*=)[\"']?([^\"'\]]+)[\"']?\]", sel)
            if not m:
                continue
            op, needle = m.group(1), m.group(2).replace("\\", "")
            for href in hrefs:
                if (op == "$=" and href.lower().endswith(needle.lower())) or \
                   (op == "*=" and needle.lower() in href.lower()):
                    found.append(href)
        return list(dict.fromkeys(found))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="все встроенные источники, а не только по ИБ")
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    from src.api.services.web_monitor import WebMonitorService

    groups = {"BUILTIN (ИБ и НПА)": WebMonitorService.BUILTIN_SOURCES}
    if args.all:
        groups.update({"GOS": WebMonitorService.GOS_SOURCES,
                       "CBR": WebMonitorService.CBR_SOURCES,
                       "SECNEWS": WebMonitorService.SECNEWS_SOURCES})
    else:
        groups["SECNEWS (ИБ-новости)"] = WebMonitorService.SECNEWS_SOURCES

    report = []
    first = True
    for group, sources in groups.items():
        print(f"\n=== {group} ===")
        for src in sources:
            if not first:
                time.sleep(PAUSE_S)          # пауза между запросами: не выглядеть DDoS
            first = False
            url = src.get("url", "")
            selector = src.get("css_selector", "")
            code, html, seconds, err = fetch(url)
            found = links(html, selector) if html else []
            row = {"name": src.get("name"), "url": url, "type": src.get("type"),
                   "code": code, "size": len(html), "seconds": round(seconds, 2),
                   "links": len(found), "sample": [f[:120] for f in found[:3]], "error": err}
            report.append(row)
            mark = "OK  " if (code == 200 and (found or src.get("type") in ("rss",))) else "ФЕЙЛ"
            print(f"  {mark} {str(src.get('name'))[:46]:<48} код {code:<4} {len(html):>7} б "
                  f"{seconds:5.2f} с  ссылок {len(found)}")
            if err:
                print(f"        ошибка: {err}")
            for f in found[:2]:
                print(f"        {f[:110]}")

            page_url = src.get("pagination_url")
            if page_url:
                time.sleep(PAUSE_S)
                pcode, phtml, psec, perr = fetch(page_url.replace("{page}", "2"))
                plinks = links(phtml, selector) if phtml else []
                print(f"        пагинация стр.2: код {pcode} {len(phtml)} б ссылок {len(plinks)}")
                report[-1]["pagination"] = {"code": pcode, "links": len(plinks),
                                            "error": perr, "seconds": round(psec, 2)}

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"\nотчёт: {args.save}")
    ok = [r for r in report if r["code"] == 200 and (r["links"] or r["type"] in ("rss",))]
    print(f"\nитог: источников {len(report)}, живых и с находками {len(ok)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
