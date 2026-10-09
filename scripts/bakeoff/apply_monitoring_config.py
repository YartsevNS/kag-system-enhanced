"""Применить на стенде правки конфигурации мониторинга (nginx + compose), точечно и с бэкапом.

Зачем: дерево развёртывания на стенде (`/home/yartsevn/kag-system`) — отдельный git на ветке PREPROD
со своими локальными правками. Перезаписывать его файлы целиком нельзя: потеряются настройки стенда.
Поэтому вносим ровно те строки, что и в репозитории, идемпотентно, с резервной копией рядом.

Запуск на стенде:  python3 apply_monitoring_config.py /home/yartsevn/kag-system
"""
from __future__ import annotations

import pathlib
import re
import shutil
import sys
from datetime import datetime

GRAFANA_LOC = """
    # ── Grafana: графики динамики (встраиваются в страницы системы) ──────
    # Проксируем через наш домен, а не открываем отдельный порт: страницы системы отдаются по HTTPS,
    # и iframe на http://host:3000 браузер заблокировал бы как смешанное содержимое.
    set $kag_grafana_upstream http://kag-grafana:3000;
    location /grafana/ {
        proxy_pass $kag_grafana_upstream;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $forwarded_proto;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
"""

# Строки окружения Grafana, которые должны быть в compose. Добавляются по одной —
# так повторный запуск скрипта докатывает недостающие, а не пропускает всё сразу.
GRAFANA_ENV_LINES = [
    "      # Анонимный доступ ТОЛЬКО на чтение и разрешённое встраивание: панели Grafana\n",
    "      # показываются внутри страниц системы (iframe через наш домен, /grafana/).\n",
    "      # Редактирование дашбордов при этом закрыто — нужен вход администратора.\n",
    "      - GF_AUTH_ANONYMOUS_ENABLED=true\n",
    "      - GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer\n",
    "      - GF_SECURITY_ALLOW_EMBEDDING=true\n",
    "      # Публичные дашборды: штатный способ показать графики без входа в Grafana\n",
    "      - GF_FEATURE_TOGGLES_ENABLE=publicDashboards\n",
    "      # Grafana живёт по подпути /grafana/, иначе ссылки в интерфейсе ведут на корень\n",
    "      - GF_SERVER_ROOT_URL=%(protocol)s://%(domain)s/grafana/\n",
    "      - GF_SERVER_SERVE_FROM_SUB_PATH=true\n",
]


def backup(path: pathlib.Path) -> pathlib.Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, dst)
    return dst


def patch_nginx(root: pathlib.Path) -> str:
    conf = root / "docker/nginx/conf.d/kag.conf"
    if not conf.exists():
        return f"nginx: файла нет ({conf})"
    text = conf.read_text(encoding="utf-8")
    if "location /grafana/" in text:
        return "nginx: уже настроено"
    # вставляем после строки с адресом keycloak — по одному разу в каждый server-блок
    pattern = re.compile(r"( *set \$kag_kc_upstream  http://keycloak:8080;\n)")
    replacements = pattern.sub(lambda m: m.group(1) + GRAFANA_LOC + "\n", text)
    count = len(pattern.findall(text))
    if count == 0:
        return "nginx: не нашёл место вставки (строки с keycloak) — правка не применена"
    backup(conf)
    conf.write_text(replacements, encoding="utf-8")
    return f"nginx: вставлено в {count} серверных блока"


def patch_compose(root: pathlib.Path) -> str:
    compose = root / "docker-compose.yml"
    if not compose.exists():
        return f"compose: файла нет ({compose})"
    text = compose.read_text(encoding="utf-8")
    missing = [line for line in GRAFANA_ENV_LINES
               if line.strip() and not line.strip().startswith("#") and line.strip() not in text]
    if not missing:
        return "compose: уже настроено"
    anchor = "      - GF_USERS_ALLOW_SIGN_UP=false\n"
    if anchor not in text:
        return "compose: не нашёл блок Grafana (GF_USERS_ALLOW_SIGN_UP)"
    backup(compose)
    compose.write_text(text.replace(anchor, anchor + "".join(GRAFANA_ENV_LINES), 1), encoding="utf-8")
    return f"compose: добавлено строк — {len(missing)}"


API_ENV_LINES = [
    "      # Grafana: нужна админская ручка «включить графики в страницах системы»\n",
    "      - GRAFANA_URL=${GRAFANA_URL:-http://kag-grafana:3000}\n",
    "      - GRAFANA_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD:-}\n",
]


def patch_api_env(root: pathlib.Path) -> str:
    """Добавить сервису api доступ к Grafana (нужен только для создания публичного дашборда)."""
    compose = root / "docker-compose.yml"
    if not compose.exists():
        return "compose: файла нет"
    text = compose.read_text(encoding="utf-8")
    missing = [line for line in API_ENV_LINES
               if line.strip() and not line.strip().startswith("#") and line.strip() not in text]
    if not missing:
        return "compose: у api доступ к Grafana уже есть"
    anchor = "      - FASTAPI_DEBUG=${FASTAPI_DEBUG:-false}\n"
    if anchor not in text:
        return "compose: не нашёл блок environment сервиса api"
    backup(compose)
    compose.write_text(text.replace(anchor, anchor + "".join(API_ENV_LINES), 1), encoding="utf-8")
    return f"compose: api — добавлено строк — {len(missing)}"


def dedupe_compose_env(root: pathlib.Path) -> str:
    """Убрать повторяющиеся строки окружения у сервиса grafana.

    Мой же скрипт при повторном запуске доставлял недостающие строки блоками, и compose начал
    ругаться «environment contains non-unique items». Здесь оставляем по одному вхождению каждой
    строки вида `- GF_...` внутри сервиса grafana (дубли в других сервисах не трогаем).
    """
    compose = root / "docker-compose.yml"
    if not compose.exists():
        return "compose: файла нет"
    lines = compose.read_text(encoding="utf-8").splitlines(keepends=True)
    out, seen = [], set()
    in_grafana = False
    removed = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("grafana:"):
            in_grafana = True
        elif stripped and not line.startswith(" ") and stripped.endswith(":"):
            in_grafana = False
        if in_grafana and stripped.startswith("- GF_") and stripped in seen:
            removed += 1
            continue
        if in_grafana and stripped.startswith("- GF_"):
            seen.add(stripped)
        out.append(line)
    if removed:
        backup(compose)
        compose.write_text("".join(out), encoding="utf-8")
    return f"compose: убрано дублей — {removed}"


def main() -> None:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/home/yartsevn/kag-system")
    print(patch_nginx(root))
    print(patch_compose(root))
    print(patch_api_env(root))
    print(dedupe_compose_env(root))


if __name__ == "__main__":
    main()
