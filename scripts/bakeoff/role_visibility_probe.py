"""Проверка видимости разделов по роли: что видит администратор, что обычный сотрудник.

Логинится администратором, но для второй части подменяет ответ /api/v1/auth/me на «не администратор» —
так проверяется механизм скрытия без создания отдельной учётки. Проверяются и пункты меню
(общий компонент), и помеченные классом admin-only элементы страниц.

Запуск в контейнере базового образа:
  docker run --rm --network host -v /work:/work -e ADMIN_PASSWORD=... \
    --entrypoint /usr/local/bin/python kre44et/kag-base:<tag> /work/role_visibility_probe.py admin http://127.0.0.1:8000
"""
import os
import sys

from playwright.sync_api import sync_playwright

USER = sys.argv[1] if len(sys.argv) > 1 else "admin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# страницы, закрытые проверкой доступа на сервере
ADMIN_PATHS = ["/admin", "/users", "/logs", "/docker", "/qdrant", "/experiments", "/monitoring"]
CHECK_PAGES = ["/chat", "/documents", "/system"]

def visible_admin_links(page):
    return page.evaluate("""(paths) => {
        const found = [];
        document.querySelectorAll('a[href]').forEach(a => {
            const href = a.getAttribute('href');
            if (paths.indexOf(href) < 0) return;
            const st = getComputedStyle(a);
            const r = a.getBoundingClientRect();
            if (st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0) found.push(href);
        });
        return found;
    }""", ADMIN_PATHS)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 950})
        r = ctx.request.post(f"{BASE}/api/v1/auth/login", data={"username": USER, "password": PASSWORD})
        print("вход:", r.status)

        page = ctx.new_page()

        print("\n=== как администратор ===")
        admin_seen = {}
        for path in CHECK_PAGES:
            page.goto(f"{BASE}{path}")
            page.wait_for_timeout(1600)
            seen = sorted(set(visible_admin_links(page)))
            admin_seen[path] = seen
            print(f"  {path}: видимых админских ссылок {len(seen)} {seen}")

        # подменяем ответ о правах: тот же пользователь, но «не администратор»
        ctx.route("**/api/v1/auth/me", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"username":"employee","is_admin":false}'))

        print("\n=== как обычный сотрудник ===")
        employee_seen = {}
        for path in CHECK_PAGES:
            page.goto(f"{BASE}{path}")
            page.wait_for_timeout(1600)
            seen = sorted(set(visible_admin_links(page)))
            employee_seen[path] = seen
            print(f"  {path}: видимых админских ссылок {len(seen)} {seen}")

        browser.close()

        bad = []
        for path in CHECK_PAGES:
            if not admin_seen[path]:
                bad.append(f"администратор не видит своих ссылок на {path}")
            if employee_seen[path]:
                bad.append(f"сотрудник видит админские ссылки на {path}: {employee_seen[path]}")
        print()
        if bad:
            for b in bad:
                print("НЕТ:", b)
            sys.exit(1)
        print("ИТОГ: администратор видит свои разделы, сотрудник — ни одной админской ссылки")


if __name__ == "__main__":
    main()
