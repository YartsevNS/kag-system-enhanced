"""Сборка каталога страниц журнала KAG и обновление заголовка раздела.

Читает список страниц через XML-RPC (core.listPages), строит страницу-каталог со ссылками
от новых к старым и обновляет kag:start. Запуск на самом хосте вики (XML-RPC открыт только
для localhost):  python3 /tmp/catalog.py
"""

import base64
import re
import urllib.request
import xml.sax.saxutils as sx

API = "http://127.0.0.1:8080/lib/exe/xmlrpc.php"
CRED = "/root/kag-agent-cred.txt"

pw = ""
for line in open(CRED, encoding="utf-8"):
    if line.startswith("пароль"):
        pw = line.split(":", 1)[1].strip()
if not pw:
    raise SystemExit("не нашёл пароль агента в " + CRED)


def call(method: str, params_xml: str) -> str:
    body = (
        '<?xml version="1.0"?><methodCall><methodName>%s</methodName><params>%s</params></methodCall>'
        % (method, params_xml)
    ).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "text/xml"})
    req.add_header("Authorization", "Basic " + base64.b64encode(("kag-agent:" + pw).encode()).decode())
    with urllib.request.urlopen(req, timeout=60) as rsp:
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


raw = call(
    "core.listPages",
    "<param><value><string>kag</string></value></param><param><value><int>2</int></value></param>",
)
ids = list(dict.fromkeys(re.findall(r"<string>(kag:[^<]*)</string>", raw)))
journal = sorted(i for i in ids if ":журнал:" in i and "каталог" not in i)


def key(page: str):
    m = re.search(r"(\d{4}-\d{2}-\d{2})(?:-(\d+))?$", page)
    return (m.group(1), int(m.group(2) or 1)) if m else ("0000", 0)


def label(page: str) -> str:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:-(\d+))?$", page)
    if not m:
        return page.split(":")[-1]
    return "%s.%s.%s%s" % (m.group(3), m.group(2), m.group(1), " (часть %s)" % m.group(4) if m.group(4) else "")


print("страниц журнала:", len(journal))

lines = [
    "====== Каталог страниц журнала KAG ======",
    "",
    "Записей всего: **%d**. Порядок — от новых к старым; внутри страницы блоки «вопрос → ответ»." % len(journal),
    "",
    "  * [[kag:start|← К разделу KAG]]",
    "",
]
for page in sorted(journal, key=key, reverse=True):
    lines.append("  * [[%s|%s]]" % (page, label(page)))
catalog = "\n".join(lines) + "\n"

recent = sorted(journal, key=key, reverse=True)[:12]
start = (
    "====== KAG: база знаний ======\n\n"
    "Раздел наполняется автоматически через API: журнал работ по проекту KAG в формате "
    "«вопрос → ответ». Записи с 14.05.2026 по настоящее время.\n\n"
    "* [[kag:каталог|Каталог страниц журнала]] — все записи\n\n"
    "Последние записи:\n\n" + "\n".join("  * [[%s|%s]]" % (p, label(p)) for p in recent) + "\n"
)

for page, text, summary in (
    ("kag:каталог", catalog, "каталог страниц журнала"),
    ("kag:start", start, "обновление заголовка раздела"),
    # Устаревшая страница-каталог первой заливки: в ней осталось «21 страница» — уводим на новый каталог.
    (
        "kag:журнал:каталог-страниц-kag-21-страница",
        "====== Каталог переехал ======\n\nЭта страница устарела (была составлена, когда в разделе было 21 страница).\n\n"
        "Актуальный каталог: [[kag:каталог]]\n",
        "каталог переехал",
    ),
):
    print(("ok   " if save(page, text, summary) else "ОШИБКА ") + page)
