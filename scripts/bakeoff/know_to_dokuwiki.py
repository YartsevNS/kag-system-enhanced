"""Перенос базы «вопрос → ответ» (know.html) в DokuWiki: разбор файла → страницы вики.

Зачем отдельный прибор. В know.html 165 блоков «вопрос → ответ» и 21 дата-секция. Переносить это руками
нельзя, а «просто залить HTML» нельзя тем более: вики работает на своём синтаксисе, и HTML-теги в
страницах выглядят как мусор. Прибор ПЕРЕВОДИТ разметку в синтаксис DokuWiki и раскладывает материал по
страницам: журнал по датам плюс индекс.

Режимы:
  --parse   — разобрать know.html и записать страницы в каталог (по умолчанию reports/_scratch/dw_pages)
  --upload  — залить страницы в вики через XML-RPC (нужны --url, --user, --password)

Разбор и заливка разделены намеренно: сначала смотрим, что получилось, потом пишем в вики.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

KNOW = Path(r"C:\VSCODE_PROJECT\know.html")
OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "_scratch" / "dw_pages"


def html_to_dokuwiki(fragment: str) -> str:
    """Перевести фрагмент HTML из know.html в синтаксис DokuWiki.

    Правила минимальные и предсказуемые: код — моноширинным шрифтом вики, жирный — жирным, списки —
    списками. Всё остальное разметкой не является и просто убирается: иначе в вики попадёт HTML-мусор.
    """
    text = fragment
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(?:div|p|span|td|tr|table|tbody)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<code[^>]*>(.*?)</code>", lambda m: "''" + m.group(1) + "''", text, flags=re.S | re.I)
    text = re.sub(r"<(?:strong|b)>(.*?)</(?:strong|b)>", lambda m: "**" + m.group(1) + "**", text,
                  flags=re.S | re.I)
    text = re.sub(r"<(?:li)[^>]*>", "  * ", text, flags=re.I)
    text = re.sub(r"</?(?:ul|ol)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)                 # прочие теги убираем
    text = html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_know(path: Path) -> list:
    """Разобрать know.html на дата-секции и блоки «вопрос → ответ».

    Разбор идёт ПО МАРКЕРАМ (split по началу блока), а не по совпадению закрывающих тегов: структура
    вложенных div-ов от записи к записи неоднородна, и попытка «поймать» правильное закрытие молча
    теряла больше половины блоков (первый заход нашёл 44 из 161).
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    sections = []
    for sec_block in re.split(r'<div class="date-section">', raw)[1:]:
        head = re.search(r'<div class="date-header">(.*?)</div>', sec_block, re.S)
        title = html.unescape(re.sub(r"<[^>]+>", "", head.group(1))).strip() if head else "без даты"
        items = []
        for chunk in sec_block.split('<div class="qa-item">')[1:]:
            texts = re.findall(r'<div class="qa-text">(.*?)</div>', chunk, re.S)
            question = html_to_dokuwiki(texts[0]) if texts else ""
            answer = html_to_dokuwiki(texts[1]) if len(texts) > 1 else ""
            if question or answer:
                items.append({"question": question, "answer": answer})
        if items:
            sections.append({"title": title, "items": items})
    return sections


def slug(title: str) -> str:
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", title.lower())
    months = {"января": "01", "февраля": "02", "марта": "03", "апреля": "04", "мая": "05", "июня": "06",
              "июля": "07", "августа": "08", "сентября": "09", "октября": "10", "ноября": "11",
              "декабря": "12"}
    if m and m.group(2) in months:
        return f"{m.group(3)}-{months[m.group(2)]}-{int(m.group(1)):02d}"
    return re.sub(r"[^0-9a-zA-Zа-яё]+", "-", title.lower())[:40].strip("-")


def build_pages(sections: list) -> dict:
    pages = {}
    index_lines = ["====== KAG: база знаний ======", "",
                   "Разборы, решения и замеры по проекту. Материал перенесён из накопительной базы "
                   "«вопрос → ответ» и разложен по датам работы.", "",
                   "===== Журнал по датам =====", ""]
    used = {}
    for sec in sections:
        # Дата не уникальна (в один день бывает несколько сессий), поэтому при повторе добавляем номер:
        # без этого страницы перезаписывают друг друга и блоки молча пропадают.
        base = slug(sec["title"]) or "без-даты"
        used[base] = used.get(base, 0) + 1
        name = base if used[base] == 1 else f"{base}-{used[base]}"
        page_id = f"kag:журнал:{name}"
        body = [f"====== {sec['title']} ======", "",
                f"Блоков «вопрос → ответ»: {len(sec['items'])}", ""]
        for i, item in enumerate(sec["items"], 1):
            q = item["question"].strip() or "(вопрос не распознан)"
            first = q.split("\n")[0][:120]
            body += [f"===== {i}. {first} =====", "", "**Вопрос:**", "", q, "",
                     "**Ответ:**", "", item["answer"] or "(ответ не распознан)", ""]
        pages[page_id] = "\n".join(body)
        index_lines.append(f"  * [[{page_id}|{sec['title']}]] — {len(sec['items'])} блоков")
    index_lines += ["", "===== Наши рабочие материалы =====", "",
                    "  * [[kag:граф:|Граф: замеры и ускорение]]",
                    "  * [[kag:ocr:|Распознавание (OCR)]]",
                    "  * [[kag:разметка:|Разметка словарями]]",
                    "  * [[kag:деплой:|Выпуск и деплой]]", ""]
    pages["kag:start"] = "\n".join(index_lines)
    return pages


def upload(url: str, user: str, password: str, pages: dict) -> int:
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    failed = 0
    for page_id, content in pages.items():
        body = ("<?xml version=\"1.0\"?><methodCall><methodName>core.savePage</methodName><params>"
                f"<param><value><string>{page_id}</string></value></param>"
                f"<param><value><string>{html.escape(content)}</string></value></param>"
                "<param><value><string>перенос базы вопрос-ответ</string></value></param>"
                "<param><value><boolean>0</boolean></value></param></params></methodCall>")
        req = urllib.request.Request(url, data=body.encode("utf-8"),
                                     headers={"Content-Type": "text/xml",
                                              "Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = r.read().decode("utf-8", "replace")
            ok = "<boolean>1</boolean>" in out or "true" in out.lower()
            print(f"  {'ok  ' if ok else 'ФЕЙЛ'} {page_id}: {out[:90]}")
            failed += 0 if ok else 1
        except Exception as e:  # noqa: BLE001
            print(f"  ФЕЙЛ {page_id}: {type(e).__name__}: {str(e)[:90]}")
            failed += 1
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--know", default=str(KNOW))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--parse", action="store_true")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--url", default="http://10.0.1.25:8080/lib/exe/xmlrpc.php")
    ap.add_argument("--user", default="kag-agent")
    ap.add_argument("--password", default="")
    args = ap.parse_args()

    sections = parse_know(Path(args.know))
    pages = build_pages(sections)
    total = sum(len(s["items"]) for s in sections)
    print(f"дата-секций: {len(sections)}; блоков «вопрос → ответ»: {total}; страниц к записи: {len(pages)}")

    if args.parse or not args.upload:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for page_id, content in pages.items():
            fname = page_id.replace(":", "_") + ".txt"
            (out / fname).write_text(content, encoding="utf-8")
        (out / "_manifest.json").write_text(
            json.dumps({k: len(v) for k, v in pages.items()}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"страницы записаны в {out}")
        print("для просмотра достаточно открыть любой .txt в этом каталоге")

    if args.upload:
        if not args.password:
            print("нужен --password (данные лежат на сервере в /root/kag-agent-cred.txt)")
            return 2
        print(f"заливаю в {args.url} от {args.user}")
        return 1 if upload(args.url, args.user, args.password, pages) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
