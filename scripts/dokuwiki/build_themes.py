"""Тематические страницы журнала KAG: связывают блоки из датированных страниц по смыслу.

Зачем: журнал хранит записи по датам (удобно читать «что делали и когда»), но по нему плохо искать
«как устроено вот это». Прибор разбирает уже залитые страницы журнала на блоки, оценивает каждый
блок по наборам слов темы и собирает страницы вида kag:темы:<тема> со ссылками на нужные записи.

Работает на хосте вики (правит файлы страниц как веб-пользователь через XML-RPC).
Запуск:  python3 build_themes.py            (проверка: печатает план)
         python3 build_themes.py --apply    (записать страницы в вики)
"""

from __future__ import annotations

import argparse
import base64
import re
import urllib.parse
import urllib.request
import xml.parsers.expat  # noqa: F401  (наличие модуля = проверка окружения)
import xml.sax.saxutils as sx
from pathlib import Path

PAGES_DIR = Path("/var/www/dokuwiki/data/pages/kag")
API = "http://127.0.0.1:8080/lib/exe/xmlrpc.php"
CRED = Path("/root/kag-agent-cred.txt")

# Наборы слов темы. Ключи — имена страниц (латиницей, чтобы ссылки были короткими),
# значения — человеческое название и наборы корней слов.
THEMES: list[tuple[str, str, list[str]]] = [
    ("граф", "Граф знаний", ["граф", "neo4j", "узл", "связей", "связи", "сущност", "извлечен",
                             "community", "graphrag", "триаж", "boilerplate", "чанк", "фрагмент"]),
    ("распознавание", "Распознавание (OCR)", ["ocr", "распозна", "скан", "paddle", "pp-ocr",
                                              "изображен", "tesseract", "страниц"]),
    ("разметка", "Разметка и словари", ["разметк", "словар", "вид документ", "рубрик", "фасет",
                                        "jev", "провенанс", "расхожден", "нормативност", "тему документ"]),
    ("таблицы", "Таблицы", ["таблиц", "xlsx", "excel", "ячеек", "csv", "табличн"]),
    ("поиск", "Поиск и ответы", ["поиск", "ретрив", "hit@", "mrr", "реранкер", "эмбеддинг",
                                 "qdrant", "выдач", "ответ на вопрос", "контекст"]),
    ("стенд", "Стенд и выкаты", ["образ", "docker", "выкат", "деплой", "health", "compose",
                                 "стенд", "сервер 18", "контейнер", "systemd", "том", "volume"]),
    ("интерфейс", "Интерфейс и страницы", ["интерфейс", "кнопк", "шрифт", "цвет", "меню", "админк",
                                           "верстк", "вёрстк", "мобильн", "светл", "темн"]),
    ("вики", "Вики и документация", ["вики", "dokuwiki", "wiki", "xmlrpc", "документац", "регламент"]),
    ("надёжность", "Надёжность, тесты, наблюдение", ["тест", "pytest", "надежн", "надёжн",
                                                      "монитор", "алерт", "метрик", "проверк", "сбой"]),
    ("процесс", "Организация работы", ["handoff", "план", "дорожн", "процесс", "правил", "память",
                                       "скилл", "отчёт", "отчет", "задач"]),
]


def page_id_from_file(path: Path) -> str:
    """Файл вики → идентификатор страницы (обратная расшифровка процентов в имени)."""
    rel = path.relative_to(PAGES_DIR).with_suffix("")
    parts = [urllib.parse.unquote(p, encoding="utf-8", errors="replace") for p in rel.parts]
    return "kag:" + ":".join(parts)


def call(method: str, params_xml: str) -> str:
    password = ""
    for line in CRED.read_text(encoding="utf-8").splitlines():
        if line.startswith("пароль"):
            password = line.split(":", 1)[1].strip()
    body = (
        '<?xml version="1.0"?><methodCall><methodName>%s</methodName><params>%s</params></methodCall>'
        % (method, params_xml)
    ).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "text/xml"})
    req.add_header("Authorization", "Basic " + base64.b64encode(("kag-agent:" + password).encode()).decode())
    with urllib.request.urlopen(req, timeout=90) as rsp:
        return rsp.read().decode("utf-8", "replace")


def save(page: str, text: str, summary: str) -> bool:
    r = call(
        "core.savePage",
        "<param><value><string>%s</string></value></param>"
        "<param><value><string>%s</string></value></param>"
        "<param><value><string>%s</string></value></param>"
        "<param><value><boolean>0</boolean></value></param>"
        % (sx.escape(page), sx.escape(text), sx.escape(summary)),
    )
    return "<boolean>1</boolean>" in r


def strip_markup(text: str) -> str:
    t = re.sub(r"\*\*|//|__|''", "", text)
    t = re.sub(r"\[\[[^|\]]*\|([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def load_blocks() -> list[dict]:
    """Все блоки журнала: дата, номер, заголовок, текст."""
    blocks: list[dict] = []
    for f in sorted(PAGES_DIR.rglob("*.txt")):
        pid = page_id_from_file(f)
        if ":журнал:" not in pid:
            continue
        raw = f.read_text(encoding="utf-8", errors="replace")
        date = re.search(r"(\d{4}-\d{2}-\d{2})", pid)
        date = date.group(1) if date else "0000-00-00"
        for m in re.finditer(r"^===== (\d+)\. (.*?) =====$(.*?)(?=^=====|\Z)", raw, re.M | re.S):
            num, title, body = m.group(1), m.group(2).strip(), m.group(3)
            body_plain = strip_markup(body)
            blocks.append({
                "page": pid, "date": date, "num": int(num),
                "title": strip_markup(title)[:110],
                "text": body_plain,
                "low": body_plain.lower(),
            })
    return blocks


def score(block: dict, roots: list[str]) -> int:
    low = block["low"]
    return sum(low.count(r) * (len(r) // 3 + 1) for r in roots if r in low)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать страницы в вики")
    ap.add_argument("--show", action="store_true", help="показать собранные страницы (для проверки)")
    ap.add_argument("--per-theme", type=int, default=15, help="сколько записей показывать на страницу темы")
    args = ap.parse_args()

    blocks = load_blocks()
    print(f"блоков в журнале: {len(blocks)}")

    theme_pages: list[tuple[str, str]] = []  # (имя страницы, текст)
    index_lines = ["====== Темы журнала ======", "",
                   "Записи журнала собраны по темам. Внутри темы — ссылки на страницы журнала, "
                   "где запись расписана целиком.", "",
                   "  * [[kag:start|← К разделу KAG]]", "", "  * [[kag:каталог|Каталог по датам]]", ""]

    for slug, title, roots in THEMES:
        scored = [(score(b, roots), b) for b in blocks]
        good = [(s, b) for s, b in scored if s > 0]
        good.sort(key=lambda x: (-x[0], x[1]["date"]))
        top = good[: args.per_theme]
        if not top:
            print(f"  тема «{title}»: ничего не нашлось")
            continue
        lines = [f"====== {title} ======", "",
                 f"Записей по теме: **{len(good)}**, показаны {len(top)} самых близких.", "",
                 "  * [[kag:темы|← Все темы]]", ""]
        for s, b in top:
            day = ".".join(reversed(b["date"].split("-")))
            short = b["title"]
            if len(short) > 80:
                cut = short[:80]
                space = cut.rfind(" ")
                short = (cut[:space] if space > 48 else cut).rstrip(" ,.;:—-") + "…"
            short = short.replace("|", "/")
            lines.append(f"  * **{day}, блок {b['num']}** — «{short}» → [[{b['page']}|открыть]]")
        theme_pages.append((f"kag:темы:{slug}", "\n".join(lines) + "\n"))
        index_lines.append(f"  * [[kag:темы:{slug}|{title}]] — записей: {len(good)}")
        print(f"  тема «{title}»: найдено {len(good)}, показано {len(top)}")

    theme_pages.append(("kag:темы", "\n".join(index_lines) + "\n"))

    if args.show:
        for page, text in theme_pages[:3]:
            print("\n" + "=" * 90)
            print(page)
            print(text[:1400])

    if not args.apply:
        print("\nэто примерка. Для записи в вики добавить --apply")
        return 0

    for page, text in theme_pages:
        print(("ok   " if save(page, text, "сборка темы") else "ОШИБКА ") + page)

    # Заголовок раздела: ставим темы на видное место
    start = (
        "====== KAG: база знаний ======\n\n"
        "Раздел наполняется автоматически: журнал работ по проекту KAG в формате «вопрос → ответ», "
        "записи с 14.05.2026 по настоящее время.\n\n"
        "**Как искать:**\n\n"
        "  * [[kag:темы|По темам]] — граф, распознавание, разметка, таблицы, поиск, стенд, интерфейс…\n"
        "  * [[kag:каталог|По датам]] — все записи от новых к старым\n\n"
        "Свежие записи удобнее смотреть в каталоге; если ищешь «как устроено вот это» — начинай с тем.\n"
    )
    print(("ok   " if save("kag:start", start, "обновление заголовка раздела") else "ОШИБКА ") + "kag:start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
