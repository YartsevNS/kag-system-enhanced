"""Генератор схемы /architecture (17 контейнеров + TypeDB-контур).

Схема собирается из описания блоков, а не правится руками в SVG: добавить
контейнер = добавить строку в BOXES, а не искать координаты в 20 КБ разметки.
"""
from __future__ import annotations
import xml.etree.ElementTree as ET

# ── палитра (совпадает с темой страницы) ─────────────────────────────────────
C = {
    "input":  "#6ee7b7",
    "app":    "#3ecf8e",
    "data":   "#8b5cf6",
    "queue":  "#f5a623",
    "sec":    "#e5484d",
    "mon":    "#b4b4b4",
    "opt":    "#898989",
    "ext":    "#898989",
    "text":   "#fafafa",
    "text2":  "#b4b4b4",
    "text3":  "#898989",
}
FILL = {
    "input": "rgba(110,231,183,0.10)",
    "app":   "rgba(62,207,142,0.12)",
    "data":  "rgba(139,92,246,0.12)",
    "queue": "rgba(245,166,35,0.12)",
    "sec":   "rgba(229,72,77,0.16)",
    "mon":   "rgba(137,137,137,0.10)",
    "ext":   "rgba(137,137,137,0.10)",
    "opt":   "rgba(137,137,137,0.06)",
}

W, H = 1400, 800

# (id, заголовок, подписи, x, y, w, h, группа)
BOXES = [
    # внешний пользователь
    ("user", "Пользователь", ["браузер · телефон", "загрузка, чат, админка"], 30, 120, 150, 80, "ext"),

    # вход и безопасность
    ("nginx",   "kag-nginx",   [":80 / :443 TLS", "проксирование"],        240, 110, 170, 60, "input"),
    ("keycloak","kag-keycloak",[":8080 · SSO", "OIDC / JWT"],              240, 190, 170, 60, "sec"),
    ("prom",    "kag-prometheus", [":9090 · метрики"],                     240, 272, 170, 56, "mon"),
    ("grafana", "kag-grafana",  [":3000 · дашборды"],                      240, 340, 170, 56, "mon"),

    # приложение
    ("api",  "kag-api", [":8000 · UI, админка, чат", "гибридный поиск + ответ"], 450, 110, 190, 90, "app"),
    ("mcp",  "kag-mcp", [":8001 · MCP-сервер продукта"],                    450, 222, 190, 50, "app"),
    ("tmcp", "kag-typedb-mcp-ro", ["MCP · онтология, только чтение"],       450, 284, 190, 50, "data"),

    # обработка
    ("worker", "kag-worker", ["Celery pool=solo", "парсинг → OCR → таблицы", "чанки → векторы"], 690, 110, 220, 96, "app"),
    ("maint",  "kag-worker-maintenance", ["граф, алиасы, сироты", "ночные задачи"],               690, 226, 220, 76, "app"),

    # данные
    ("pg",     "kag-postgres", ["документы, JSONB-таблицы,", "настройки, чаты,", "очередь предложений"], 950, 100, 210, 76, "data"),
    ("qdrant", "kag-qdrant",   ["векторы 2560 (GigaR)", "+ sparse BM25 → RRF"],                           950, 188, 210, 60, "data"),
    ("redis",  "kag-redis",    ["брокер Celery + кэш"],                                                   950, 260, 210, 50, "queue"),
    ("neo4j",  "kag-neo4j",    ["граф знаний (DozerDB)", "сущности, связи, алиасы"],                       950, 322, 210, 60, "data"),
    ("typedb", "kag-typedb",   ["онтология 3.14:", "контур корпуса +", "контур доказательств"],            950, 394, 210, 76, "data"),
    ("silo",   "kag-silo",     ["S3 · оригиналы документов"],                                             950, 482, 210, 50, "data"),
    ("omaxio", "kag-openmaxio-browser", ["веб-консоль S3"],                                               950, 544, 210, 44, "mon"),

    # внешние сервисы (вне ВМ 109)
    ("srv41",  "Сервер моделей 41", ["ВМ 101 · CPU, без GPU", "OCR :8020 CPU / :8021 OpenVINO", "Ollama :11434 (VL, опция)", "реранкер :8010"], 1180, 100, 200, 130, "opt"),
    ("giga",   "kag-gigachat-proxy", ["шлюз к LLM: чат,", "анализ, эмбеддинги"], 1180, 260, 200, 70, "opt"),
    ("extllm", "внешние LLM", ["провайдеры (опция)", "в закрытом контуре — нет"], 1180, 356, 200, 60, "opt"),
]

# Стрелки с фиксированными координатами (левая часть схемы, внешние связи)
ARROWS = [
    (180, 160, 238, 140, "input", "HTTPS", 209, 132, None),
    (410, 140, 448, 140, "input", "proxy", 429, 132, None),
    (450, 175, 412, 205, "sec", "OIDC / JWT", 424, 196, "5,4"),
    (640, 120, 1178, 285, "opt", "LLM", 910, 205, "5,4"),
    (910, 118, 1178, 160, "opt", "OCR строк и ячеек", 1040, 130, None),
    (910, 140, 1178, 205, "opt", "VL-модель (опция)", 1050, 185, "5,4"),
    (640, 158, 1178, 222, "opt", "реранк (поиск)", 900, 214, "3,3"),
    (410, 297, 448, 220, "mon", "метрики", 420, 262, "3,3"),
    (1280, 330, 1280, 354, "opt", "", 0, 0, None),
]

# Связи «источник → хранилище»: координаты вычисляются по фактическим блокам,
# чтобы после изменения высот стрелки не уезжали в пустоту.
# (id источника, id цели, подпись, класс, пунктир, сдвиг старта)
DATA_LINKS = [
    ("api",    "pg",     "SQL",            "data", None, -28),
    ("api",    "qdrant", "поиск",          "data", None, -10),
    ("api",    "neo4j",  "связи",          "data", None, 8),
    ("api",    "typedb", "факты (ACL)",    "data", None, 26),
    ("worker", "pg",     "",               "data", None, -26),
    ("worker", "qdrant", "",               "data", None, -8),
    ("worker", "redis",  "Celery",         "queue", None, 0),
    ("worker", "neo4j",  "",               "data", None, 10),
    ("worker", "typedb", "факты + провенанс", "data", None, 28),
    ("worker", "silo",   "",               "data", None, 44),
    ("maint",  "neo4j",  "",               "data", None, -6),
    ("maint",  "typedb", "",               "data", None, 14),
    ("tmcp",   "typedb", "",               "data", None, 0),
    ("silo",   "omaxio", "",               "mon",  None, 0),
]

GROUPS = [
    ("ВМ 109 · стенд 18 · docker-compose (17 контейнеров)", 210, 40, 950, 640, "app"),
    ("Сервер 41 · ВМ 101 · CPU", 1180, 90, 200, 150, "opt"),
]


def fit_boxes(boxes):
    """Высота блока — по числу строк, колонка данных — сверху вниз без наложений.

    Без этого подписи вылезают за рамку, а блоки в колонке хранилищ наезжают
    друг на друга: высота у них разная из-за разного числа строк.
    """
    fixed = []
    for bid, title, lines, x, y, w, h, cls in boxes:
        need = 40 + 13 * len(lines) + 8
        fixed.append((bid, title, lines, x, y, w, max(h, need), cls))

    # колонка хранилищ (x >= 950 и внутри ВМ 109) — пересчитываем y подряд
    data_col = [b for b in fixed if 950 <= b[3] < 1180]
    others = [b for b in fixed if not (950 <= b[3] < 1180)]
    y = 100
    laid = []
    for bid, title, lines, x, w_, w, h, cls in [(b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]) for b in data_col]:
        laid.append((bid, title, lines, x, y, w, h, cls))
        y += h + 10
    return others + laid


LEGEND = [
    ("вход (nginx)", "input"),
    ("приложение", "app"),
    ("данные и онтология", "data"),
    ("очередь и кэш", "queue"),
    ("безопасность", "sec"),
    ("мониторинг", "mon"),
    ("опционально", "opt"),
]


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build() -> str:
    out = []
    a = out.append
    a(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
      f'font-family="\'Cascadia Code\',\'Fira Code\',Consolas,monospace" role="img" '
      f'aria-label="Архитектура стенда KAG: 17 контейнеров, потоки загрузки, чата и онтологии">')
    a('<defs>')
    a('<marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" '
      'orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="#898989"/></marker>')
    a('<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">'
      '<path d="M 40 0 L 0 0 0 40" fill="none" stroke="#242424" stroke-width="0.5"/></pattern>')
    a('</defs>')
    a(f'<rect width="100%" height="100%" fill="url(#grid)"/>')

    # группы
    for title, x, y, w, h, cls in GROUPS:
        stroke = C[cls]
        a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="rgba(62,207,142,0.03)" '
          f'stroke="{stroke}" stroke-opacity="0.5" stroke-width="1" stroke-dasharray="8,4"/>')
        a(f'<text x="{x + 14}" y="{y + 20}" fill="{stroke}" font-size="10" '
          f'font-weight="600">{esc(title)}</text>')

    # стрелки (рисуем до блоков, чтобы не перекрывать текст)
    boxes = fit_boxes(BOXES)
    by_id = {b[0]: b for b in boxes}

    for x1, y1, x2, y2, cls, label, lx, ly, dash in ARROWS:
        style = f' stroke-dasharray="{dash}"' if dash else ""
        a(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{C[cls]}" '
          f'stroke-width="1.4"{style} marker-end="url(#arrowhead)"/>')
        if label:
            a(f'<text x="{lx}" y="{ly}" fill="{C["text3"]}" font-size="8" '
              f'text-anchor="middle">{esc(label)}</text>')

    # связи к хранилищам — по фактическим краям блоков
    for src_id, dst_id, label, cls, dash, shift in DATA_LINKS:
        s, d = by_id[src_id], by_id[dst_id]
        x1, y1 = s[3] + s[5], s[4] + s[6] / 2 + shift * 0.4
        x2, y2 = d[3], d[4] + d[6] / 2
        style = f' stroke-dasharray="{dash}"' if dash else ""
        if abs(x2 - x1) < 4:          # вертикальная связь (silo → консоль)
            y1, y2 = s[4] + s[6], d[4]
            x1 = x2 = s[3] + s[5] / 2
        a(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="{C[cls]}" '
          f'stroke-width="1.4"{style} marker-end="url(#arrowhead)"/>')
        if label:
            a(f'<text x="{(x1 + x2) / 2:.0f}" y="{min(y1, y2) - 6:.0f}" fill="{C["text3"]}" '
              f'font-size="8" text-anchor="middle">{esc(label)}</text>')

    # блоки
    for bid, title, lines, x, y, w, h, cls in boxes:
        stroke = C[cls]
        a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="#0f0f0f" stroke="none"/>')
        a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{FILL[cls]}" '
          f'stroke="{stroke}" stroke-width="1.5"/>')
        cx = x + w / 2
        a(f'<text x="{cx}" y="{y + 22}" fill="{C["text"]}" font-size="11" font-weight="600" '
          f'text-anchor="middle">{esc(title)}</text>')
        ly = y + 40
        for ln in lines:
            a(f'<text x="{cx}" y="{ly}" fill="{C["text2"]}" font-size="8.5" '
              f'text-anchor="middle">{esc(ln)}</text>')
            ly += 13

    # легенда
    ly = 712
    a(f'<text x="210" y="{ly}" fill="{C["text3"]}" font-size="9" font-weight="600">Легенда</text>')
    lx = 210
    ly += 18
    for name, cls in LEGEND:
        a(f'<circle cx="{lx + 4}" cy="{ly - 3}" r="4" fill="{C[cls]}"/>')
        a(f'<text x="{lx + 14}" y="{ly}" fill="{C["text2"]}" font-size="9">{esc(name)}</text>')
        lx += 22 + len(name) * 6.2

    # подпись под схемой
    a(f'<text x="210" y="{ly + 24}" fill="{C["text3"]}" font-size="8">'
      f'Потоки: nginx → api → worker → (Qdrant · Neo4j · TypeDB · S3); '
      f'чат: поиск → ACL → Neo4j (связи) → TypeDB (факты) → LLM</text>')
    a('</svg>')
    return "".join(out)


if __name__ == "__main__":
    svg = build()
    ET.fromstring(svg)  # падаем сразу, если разметка битая
    print("SVG OK, символов:", len(svg))
    with open("C:/Users/yartsev.ns/AppData/Local/hermes/cache/scratch/architecture-new.svg",
              "w", encoding="utf-8") as f:
        f.write(svg)
