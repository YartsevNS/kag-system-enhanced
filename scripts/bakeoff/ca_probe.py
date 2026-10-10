"""Тест: лечится ли доступ к fstec.ru добавлением российского корневого сертификата.

Проблема, найденная пробой источников: сертификат fstec.ru выпущен «Russian Trusted Sub CA» (Минцифры),
а в хранилище контейнера (Debian, 150 сертификатов) российских корней нет — проверка падает
`unable to get local issuer certificate`, и источник мёртв целиком (скачивать нечего).

Проверка по шагам: собрать комплект из системных сертификатов и скачанных российских, затем открыть
страницу с ЭТИМ комплектом. Если открывается — лечение известно (вшить сертификаты в образ), если
нет — проблема глубже (например, сервер не отдаёт промежуточный), и в отчёте должно стоять это.

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/ca_probe.py --cafile /tmp/bundle.pem
"""
from __future__ import annotations

import argparse
import re
import ssl
import sys
import urllib.request

URLS = ("https://fstec.ru/dokumenty",)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cafile", required=True)
    ap.add_argument("--urls", default=",".join(URLS))
    args = ap.parse_args()

    ctx = ssl.create_default_context(cafile=args.cafile)
    for url in [u.strip() for u in args.urls.split(",") if u.strip()]:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; KAG/0.1)"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                body = r.read()
                text = body.decode("utf-8", "replace")
            docs = len(re.findall(r'href="[^"]+\.(?:pdf|doc|docx|rtf)"', text, re.I))
            files = len(re.findall(r'href="[^"]*(?:file/load|File/)[^"]*"', text, re.I))
            print(f"ОТКРЫЛОСЬ: код {r.status}, {len(body)} байт, файлов по расширению {docs}, "
                  f"по служебному пути {files}  {url}")
        except Exception as e:  # noqa: BLE001
            print(f"НЕ ОТКРЫЛОСЬ: {type(e).__name__}: {str(e)[:150]}  {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
