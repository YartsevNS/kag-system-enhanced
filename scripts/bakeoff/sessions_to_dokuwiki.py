"""Перенос журнала сессий Hermes в DokuWiki — продолжение базы «вопрос → ответ» после 17.06.2026.

Зачем: файл know.html перестал пополняться после 20.06.2026, а работа продолжалась
(июль — октябрь 2026). Материал остался только в журнале сессий Hermes (state.db).
Прибор собирает пары «вопрос пользователя» → «ответ агента» из журнала и раскладывает их
в страницы DokuWiki в том же формате, что и ранее перенесённые (kag:журнал:ГГГГ-ММ-ДД).

Важно: текст берётся ДОСЛОВНО из журнала (никаких пересказов и выдуманных чисел).
Длинные ответы подрезаются по абзацам, середина не выдумывается — вместо этого ставится
явная пометка о сокращении.

Запуск:
  python scripts/bakeoff/sessions_to_dokuwiki.py --db <копия state.db> --out <каталог>
  python scripts/bakeoff/sessions_to_dokuwiki.py --db ... --out ... --upload \
      --url http://127.0.0.1:8080/lib/exe/xmlrpc.php --user kag-agent --password <пароль>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.request
from pathlib import Path

# Вопросы, которые не несут работы: приветствия, подтверждения, служебные вставки.
TRIVIAL = re.compile(
    r"^\s*(?:привет|здравствуй\w*|добрый\s+\w+|ок|окей|да|нет|ага|угу|спасибо|благодарю|"
    r"понятно|понял|хорошо|ладно|продолжай|продолжи|дальше|давай|жди|жди меня|"
    r"[\W_]{0,6}|"
    r"можешь\?|а\?|что\?|how are you|hi|hello|привет\W*)\s*$",
    re.IGNORECASE | re.UNICODE,
)
# Служебные вставки в роль пользователя (не его слова).
INJECTED = re.compile(
    r"^\s*\[(?:IMPORTANT|OUT-OF-BAND|Context|Note|System|Tool|Memory|Mnemosyne|"
    r"You have reached|Background process)|"
    r"You've reached the maximum number of tool-calling iterations|"
    r"Please provide a final response summarizing",
    re.IGNORECASE,
)
GREETING_TITLES = re.compile(
    r"^(?:Friendly greeting|Приветствие|Начать разговор|Ответить одним словом|"
    r"Узнать версию модели|Узнать модель|Здравствуй)",
    re.IGNORECASE,
)


def clean(text: str) -> str:
    """Убираем служебную обвязку Hermes и лишние пустые строки."""
    t = text or ""
    t = re.sub(r"<memory-context>.*?</memory-context>", "", t, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"\[Context from the interrupted assistant response\].*?\n\n", "", t, flags=re.DOTALL)
    t = re.sub(r"\[Tool loop warning:[^\]]*\]", "", t)
    t = re.sub(r"\[response interrupted\]", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def trim_body(text: str, limit: int) -> str:
    """Режем длинный ответ по абзацам. Середину НЕ выдумываем — ставим пометку."""
    t = text.strip()
    if len(t) <= limit:
        return t
    keep = t[:limit]
    cut = max(keep.rfind("\n\n"), keep.rfind("\n"))
    if cut > limit * 0.4:
        keep = keep[:cut]
    return keep.rstrip() + "\n\n_…ответ сокращён для страницы вики; полный текст — в журнале сессий._"


def dokuwiki_escape(text: str) -> str:
    """Готовим текст для DokuWiki: заголовки и таблицы не должны сломать разметку."""
    out = text
    out = re.sub(r"^(\s*)(#{1,6})\s*", r"\1**", out, flags=re.MULTILINE)  # markdown-заголовки не нужны
    out = re.sub(r"</?(?:div|span|p|br|ul|ol|li|code|pre)[^>]*>", "", out, flags=re.IGNORECASE)
    out = out.replace("&nbsp;", " ")
    out = re.sub(r"^=+(.*?)=+\s*$", r"**\1**", out, flags=re.MULTILINE)  # не создаём заголовки вики
    return out.strip()


def load_sessions(db: Path, since: str, skip_subagents: bool) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out: list[dict] = []
    for s in con.execute(
        "SELECT id, title, started_at, message_count FROM sessions ORDER BY started_at"
    ):
        started = dt.datetime.fromtimestamp(float(s["started_at"]))
        stamp = started.strftime("%Y-%m-%d")
        if stamp < since:
            continue
        title = str(s["title"] or "")
        if skip_subagents and title.startswith("Subagent:"):
            continue
        msgs = []
        for m in con.execute(
            "SELECT id, role, content, tool_calls, active FROM messages "
            "WHERE session_id=? AND active=1 ORDER BY id",
            (s["id"],),
        ):
            role = m["role"]
            if role not in ("user", "assistant"):
                continue
            text = clean(m["content"] if isinstance(m["content"], str) else "")
            if not text:
                continue
            if role == "user" and INJECTED.search(text):
                continue
            msgs.append({"role": role, "text": text, "tools": bool(m["tool_calls"])})
        out.append({"id": s["id"], "title": title, "date": stamp, "started": started, "messages": msgs})
    con.close()
    return out


def pairs(session: dict, body_limit: int) -> list[dict]:
    """Пары «вопрос → итог хода».

    Важно: ответом берём ПОСЛЕДНЮЮ реплику агента перед следующим вопросом — это то, чем ход
    закончился (отчёт: что сделано, числа, коммиты). Первая реплика хода — обычно план
    («сейчас посмотрю»), она в вики бесполезна.
    """
    msgs = session["messages"]
    items: list[dict] = []
    n = len(msgs)
    for i, m in enumerate(msgs):
        if m["role"] != "user":
            continue
        q = m["text"]
        if TRIVIAL.match(q) or len(q) < 12:
            continue
        turn: list[str] = []
        for j in range(i + 1, n):
            if msgs[j]["role"] == "user":
                break
            turn.append(msgs[j]["text"])
        if not turn:
            continue
        ans = turn[-1]
        if len(ans) < 120 and len(turn) > 1:
            ans = max(turn, key=len)  # ход закончился короткой репликой — берём самую содержательную
        if not ans.strip():
            continue
        items.append({"q": q, "a": ans})
    return items


def shorten(text: str, limit: int) -> str:
    """Обрезаем заголовок по границе слова, чтобы не рвать фразу на полуслове."""
    t = " ".join(text.split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,.;:—-") + "…"


def build_page(day_iso: str, sessions: list[tuple[dict, list[dict]]], body_limit: int,
               part_no: int = 0, part_total: int = 1) -> str:
    day = dt.datetime.strptime(day_iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    blocks = sum(len(items) for _, items in sessions)
    suffix = f" (часть {part_no} из {part_total})" if part_total > 1 else ""
    head = f"====== {day} — журнал работ{suffix} ======\n\n"
    head += f"Блоков «вопрос → ответ»: {blocks}\n\n"
    body = []
    k = 0
    for sess, items in sessions:
        for it in items:
            k += 1
            q = dokuwiki_escape(it["q"])[:1200]
            a = dokuwiki_escape(trim_body(it["a"], body_limit))
            title = shorten(q.splitlines()[0], 90)
            body.append(f"===== {k}. {title} =====\n\n**Вопрос:**\n\n{q}\n\n**Ответ:**\n\n{a}\n")
    return head + "\n".join(body)


def split_parts(items: list[tuple[dict, list[dict]]], part_chars: int, body_limit: int) -> list[list[tuple[dict, list[dict]]]]:
    """Делим день на части по блокам (не по сессиям), чтобы страница была подъёмной для браузера.

    Крупная одиночная сессия тоже обязана делиться — иначе получаются страницы на 100 тысяч знаков.
    """
    parts: list[list[tuple[dict, list[dict]]]] = [[]]
    size = 0
    for sess, its in items:
        for it in its:
            bsize = len(it["a"][:body_limit]) + len(it["q"][:1200])
            if size and size + bsize > part_chars:
                parts.append([])
                size = 0
            if parts[-1] and parts[-1][-1][0] is sess:
                parts[-1][-1][1].append(it)  # продолжаем ту же сессию в этой части
            else:
                parts[-1].append((sess, [it]))
            size += bsize
    return [p for p in parts if p]



def xml_safe(text: str) -> str:
    """Готовим текст для XML-RPC: экранируем служебные символы и убираем недопустимые.

    Без этого страницы со знаком & или < ломают запрос — вики отвечает ошибкой разбора,
    и заливка «молча» не проходит (проверено на странице «Duplicates & Versioning»).
    """
    import xml.sax.saxutils as sx

    cleaned = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 0x20)
    return sx.escape(cleaned)


def submit(url: str, user: str, password: str, page: str, text: str, summary: str) -> bool:
    payload = (
        '<?xml version="1.0"?><methodCall><methodName>core.savePage</methodName><params>'
        f"<param><value><string>{xml_safe(page)}</string></value></param>"
        f"<param><value><string>{xml_safe(text)}</string></value></param>"
        f"<param><value><string>{xml_safe(summary)}</string></value></param>"
        "<param><value><boolean>0</boolean></value></param></params></methodCall>"
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "text/xml"})
    import base64

    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=60) as rsp:
            body = rsp.read().decode("utf-8", "replace")
        if "<boolean>1</boolean>" in body:
            return True
        print(f"    отказ вики: {body.strip()[:200]}")
        return False
    except Exception as exc:  # noqa: BLE001 — печатаем причину, не глотаем
        print(f"    ошибка отправки: {exc}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="копия state.db журнала сессий")
    ap.add_argument("--since", default="2026-06-18", help="с какой даты брать сессии")
    ap.add_argument("--out", default="", help="куда положить страницы (по умолчанию — во временный каталог)")
    ap.add_argument("--body-limit", type=int, default=2600, help="предел длины ответа на странице")
    ap.add_argument("--part-chars", type=int, default=45000, help="предел размера страницы в знаках")
    ap.add_argument("--min-blocks", type=int, default=2, help="не заводить страницу, если блоков меньше")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--push-dir", default="", help="залить уже готовые страницы из каталога (без журнала)")
    ap.add_argument("--url", default="http://127.0.0.1:8080/lib/exe/xmlrpc.php")
    ap.add_argument("--prefix", default="kag:журнал:", help="префикс имён страниц")
    ap.add_argument("--user", default="kag-agent")
    ap.add_argument("--password", default=os.environ.get("DOKUWIKI_PASSWORD", ""))
    args = ap.parse_args()

    if args.push_dir:
        src = Path(args.push_dir)
        files = sorted(p for p in src.glob("*.txt") if not p.name.startswith("_"))
        if not files:
            print(f"в каталоге нет страниц: {src}")
            return 2
        if not args.password:
            print("нет пароля: задать --password или DOKUWIKI_PASSWORD")
            return 2
        print(f"=== ЗАЛИВКА ГОТОВЫХ СТРАНИЦ ({len(files)}) ===")
        ok = 0
        for f in files:
            page = args.prefix + f.stem
            text = f.read_text(encoding="utf-8")
            if submit(args.url, args.user, args.password, page, text, f"журнал работ: {f.stem}"):
                ok += 1
                print(f"  ok   {page}  ({len(text)} знаков)")
            else:
                print(f"  ОШИБКА {page}")
        print(f"\nзалито: {ok} из {len(files)}")
        return 0 if ok == len(files) else 1

    db = Path(args.db)
    if not db.exists():
        print(f"нет файла журнала: {db}")
        return 2
    out_dir = Path(args.out) if args.out else Path(os.environ.get("TEMP", "/tmp")) / "dw_sessions"
    out_dir.mkdir(parents=True, exist_ok=True)

    sessions = load_sessions(db, args.since, skip_subagents=True)
    print(f"сессий с {args.since}: {len(sessions)}\n")

    by_day: dict[str, list[tuple[dict, list[dict]]]] = {}
    for s in sessions:
        items = pairs(s, args.body_limit)
        if GREETING_TITLES.match(s["title"]) and len(items) < 3:
            continue
        good = [it for it in items if len(it["a"]) >= 120]  # осмысленный ответ, а не «ок»
        if len(good) < args.min_blocks:
            print(f"  пропуск  {s['date']}  {s['title'][:40]:42s} блоков {len(good)}")
            continue
        by_day.setdefault(s["date"], []).append((s, good))
        print(f"  готово   {s['date']}  {s['title'][:40]:42s} блоков {len(good):3d}")

    plan = []
    for day in sorted(by_day):
        parts = split_parts(by_day[day], args.part_chars, args.body_limit)
        for idx, part in enumerate(parts, 1):
            name = day if len(parts) == 1 else f"{day}-{idx}"
            page = args.prefix + name
            text = build_page(day, part, args.body_limit, idx, len(parts))
            (out_dir / (name + ".txt")).write_text(text, encoding="utf-8")
            blocks = sum(len(i) for _, i in part)
            plan.append({"page": page, "date": day, "file": name, "blocks": blocks, "chars": len(text)})
            print(f"  страница {name}  блоков {blocks:3d}  {len(text):6d} знаков")

    total = sum(p["blocks"] for p in plan)
    print(f"\nитого страниц: {len(plan)}, блоков: {total}")
    (out_dir / "_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"каталог разбора: {out_dir}")

    if not args.upload:
        print("\nэто примерка (без заливки). Для заливки добавить --upload и пароль.")
        return 0
    if not args.password:
        print("нет пароля: задать --password или DOKUWIKI_PASSWORD")
        return 2

    print("\n=== ЗАЛИВКА В ВИКИ ===")
    ok = 0
    for p in plan:
        text = (out_dir / (p["date"] + ".txt")).read_text(encoding="utf-8")
        if submit(args.url, args.user, args.password, p["page"], text, f"журнал: {p['date']} ({p['blocks']} блоков)"):
            ok += 1
            print(f"  ok   {p['page']}")
        else:
            print(f"  ОШИБКА {p['page']}")
    print(f"\nзалито страниц: {ok} из {len(plan)}")
    return 0 if ok == len(plan) else 1


if __name__ == "__main__":
    raise SystemExit(main())
