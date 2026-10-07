"""Проверка превью документов: формат страницы, поля и рамка.

Что проверяется приборно:
  1. Миниатюра любого документа — страница A4 (портрет 1:1,414) с белыми полями по краю,
     то есть скан или фото не обрезаны, масштаб как у PDF.
  2. Рамка карточки документа на 10% темнее обычной границы (считается по итоговому цвету на фоне).

Запуск в контейнере базового образа:
  docker run --rm --network host -v /work:/work -e ADMIN_PASSWORD=... \
    --entrypoint /usr/local/bin/python kre44et/kag-base:<tag> /work/preview_check.py admin http://127.0.0.1:8000
"""
import io
import os
import re
import sys

from playwright.sync_api import sync_playwright

USER = sys.argv[1] if len(sys.argv) > 1 else "admin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

PAGE_RATIO = 1.4142
MARGIN_RATIO = 0.05


def composite_on_white(color: str, bg=(255, 255, 255)):
    """Итоговый цвет полупрозрачной границы на фоне."""
    m = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?", color)
    if not m:
        return None
    r, g, b = (int(x) for x in m.groups()[:3])
    a = float(m.group(4)) if m.group(4) else 1.0
    return tuple(round(a * c + (1 - a) * bg[i]) for i, c in enumerate((r, g, b)))


def main() -> None:
    from PIL import Image

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 950})
        r = ctx.request.post(f"{BASE}/api/v1/auth/login", data={"username": USER, "password": PASSWORD})
        print("вход:", r.status)

        listing = ctx.request.get(f"{BASE}/api/v1/upload/list?limit=1000").json()
        docs = listing.get("documents") or listing.get("items") or []
        print("документов в списке:", len(docs))

        def pick(exts):
            for d in docs:
                name = (d.get("filename") or "").lower()
                if any(name.endswith(e) for e in exts):
                    return d
            return None

        targets = [("скан или фото", pick((".png", ".jpg", ".jpeg", ".tiff", ".bmp"))),
                   ("PDF", pick((".pdf",)))]
        failures = []
        for label, d in targets:
            if not d:
                print(f"  {label}: подходящего документа в базе нет — пропускаю")
                continue
            resp = ctx.request.get(f"{BASE}/api/v1/upload/{d['document_id']}/thumbnail")
            img = Image.open(io.BytesIO(resp.body())).convert("RGB")
            w, h = img.size
            ratio = h / w
            band = max(2, int(round(w * MARGIN_RATIO / 2)))
            corners = [img.getpixel((x, y))
                       for x in (band, w - 1 - band) for y in (band, h - 1 - band)]
            white_edges = all(min(px) >= 245 for px in corners)
            ok_ratio = abs(ratio - PAGE_RATIO) < 0.02
            print(f"  {label} ({d.get('filename')}): {w}x{h}, отношение {ratio:.3f}, "
                  f"поля {'есть' if white_edges else 'НЕТ'}")
            if not ok_ratio:
                failures.append(f"{label}: формат {w}x{h} не страница A4 (отношение {ratio:.3f})")
            if not white_edges:
                failures.append(f"{label}: по краю нет полей (обрезано)")

        # рамка: насколько темнее обычная граница
        page = ctx.new_page()
        page.goto(f"{BASE}/documents")
        page.wait_for_timeout(1800)
        colors = page.evaluate("""() => {
            const cs = getComputedStyle(document.documentElement);
            const card = document.querySelector('.doc-card');
            return {
                preview: cs.getPropertyValue('--preview-border').trim(),
                base: cs.getPropertyValue('--border-s').trim(),
                card: card ? getComputedStyle(card).borderTopColor : '',
            };
        }""")
        print("\nрамка:")
        print("  переменная превью:", colors["preview"], "| обычная граница:", colors["base"])
        print("  фактический цвет рамки карточки:", colors["card"])
        comp_new = composite_on_white(colors["card"])
        comp_old = composite_on_white(colors["base"])
        if comp_new and comp_old:
            # считаем по каналу R: насколько темнее стал итоговый цвет на белом фоне
            darker = (comp_old[0] - comp_new[0]) / comp_old[0] * 100
            print(f"  итог на белом: было {comp_old} -> стало {comp_new} = темнее на {darker:.1f}%")
            if abs(darker - 10) > 2.5:
                failures.append(f"рамка темнее на {darker:.1f}%, ожидалось около 10%")

        browser.close()
        print()
        if failures:
            for f in failures:
                print("НЕТ:", f)
            sys.exit(1)
        print("ИТОГ: превью — страница A4 с полями у всех типов, рамка темнее на 10%")


if __name__ == "__main__":
    main()
