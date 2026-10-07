"""Проверка гипотезы «после ответа исчезает меню» на живой странице чата.

Механизм: .app — flex, .sidebar — фиксированные 280px, .main — flex:1 БЕЗ min-width:0.
Широкий контент в ответе (таблица, длинная строка без пробелов, URL) заставляет .main расти,
страница получает горизонтальную прокрутку, и сайдбар уезжает за левый край — «меню исчезло».

Что делает скрипт:
  1) логинится тем же способом, что браузер (cookie kag_token),
  2) открывает /chat, снимает замеры и скриншот ДО,
  3) вставляет в #messages широкую таблицу и длинную строку (то, что приходит в ответах),
  4) снимает замеры и скриншот ПОСЛЕ и печатает вердикт.

Запуск внутри образа kag-base (там Chromium):
  docker run --rm -v /tmp/kagchat:/work --entrypoint /usr/local/bin/python kre44et/kag-base:<tag> \
      /work/chat_layout_probe.py <admin_username> <base_url>
"""
import os
import sys

from playwright.sync_api import sync_playwright

USER = sys.argv[1] if len(sys.argv) > 1 else "admin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
OUT = os.environ.get("OUT_DIR", "/work")

PROBE_JS = """() => {
  const sb = document.querySelector('.sidebar, nav');
  const rect = sb ? sb.getBoundingClientRect() : null;
  return {
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    sidebar: sb ? {
      display: getComputedStyle(sb).display,
      left: rect.left, right: rect.right, width: rect.width, visible: rect.width > 0 && rect.left >= 0
    } : null,
    messagesScrollWidth: (() => { const m = document.getElementById('messages'); return m ? m.scrollWidth : null; })(),
    widestEl: (() => {
      let worst = null;
      document.querySelectorAll('#messages *').forEach(e => {
        const w = e.getBoundingClientRect().width;
        if (!worst || w > worst.w) worst = {w: Math.round(w), tag: e.tagName};
      });
      return worst;
    })()
  };
}"""

WIDE_HTML = """
<div class="msg assistant"><div class="bubble"><div class="md">
<p>Пример широкого ответа: таблица и длинная строка без пробелов.</p>
<table><tr><th>Наименование работ</th><th>Ед.</th><th>Кол-во</th><th>Цена</th><th>Сумма</th>
<th>Примечание по объекту</th><th>Дополнительная колонка</th><th>Ещё колонка</th></tr>
<tr><td>Монтаж кабеля UTP cat.6 в гофре по стене под потолком, с креплением и маркировкой</td>
<td>м</td><td>120</td><td>450,00</td><td>54 000,00</td><td>объект Лотте</td><td>этап 1</td><td>согласовано</td></tr></table>
<p>Ссылка: https://qd.gostsecret.ru/viewer?id=676f6621-7f65-4dc8-a103-1ae310a5054c&page=12&q=%D1%81%D0%BC%D0%B5%D1%82%D0%B0&highlight=1</p>
</div></div></div>
"""


def measure(page, label: str) -> None:
    data = page.evaluate(PROBE_JS)
    print(f"\n--- {label} ---")
    print(f"  страница: scrollWidth {data['scrollWidth']} против clientWidth {data['clientWidth']}"
          f" → горизонтальная прокрутка: {'ЕСТЬ' if data['scrollWidth'] > data['clientWidth'] + 1 else 'нет'}")
    sb = data["sidebar"]
    if sb:
        print(f"  меню: display={sb['display']}, left={sb['left']:.0f}, width={sb['width']:.0f},"
              f" видно целиком: {'да' if sb['visible'] else 'НЕТ'}")
    else:
        print("  меню: не найдено на странице")
    print(f"  ширина области сообщений: {data['messagesScrollWidth']}, самый широкий элемент в ней: {data['widestEl']}")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for width in (1440, 800, 760):
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            if PASSWORD:
                resp = ctx.request.post(f"{BASE}/api/v1/auth/login",
                                        data={"username": USER, "password": PASSWORD})
                if resp.status != 200:
                    print(f"[{width}px] вход не удался: HTTP {resp.status} — проверю страницу как есть")
            page = ctx.new_page()
            page.goto(f"{BASE}/chat")
            page.wait_for_timeout(2500)
            print(f"\n=================== ширина окна {width}px ===================")
            measure(page, "ДО ответа")
            page.screenshot(path=f"{OUT}/chat_{width}_before.png", full_page=False)

            page.evaluate("""(html) => {
                const m = document.getElementById('messages');
                if (m) { m.innerHTML = html; }
            }""", WIDE_HTML)
            page.wait_for_timeout(600)
            measure(page, "ПОСЛЕ широкого ответа")
            page.screenshot(path=f"{OUT}/chat_{width}_after.png", full_page=False)
            ctx.close()
        browser.close()
    print("\nскриншоты: %s/chat_<ширина>_{before,after}.png" % OUT)


if __name__ == "__main__":
    main()
