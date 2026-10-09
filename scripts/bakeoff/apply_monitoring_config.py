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

GRAFANA_ENV = """      # Анонимный доступ ТОЛЬКО на чтение и разрешённое встраивание: панели Grafana
      # показываются внутри страниц системы (iframe через наш домен, /grafana/).
      # Редактирование дашбордов при этом закрыто — нужен вход администратора.
      - GF_AUTH_ANONYMOUS_ENABLED=true
      - GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
      - GF_SECURITY_ALLOW_EMBEDDING=true
      # Grafana живёт по подпути /grafana/, иначе ссылки в интерфейсе ведут на корень
      - GF_SERVER_ROOT_URL=%(protocol)s://%(domain)s/grafana/
      - GF_SERVER_SERVE_FROM_SUB_PATH=true
"""


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
    if "GF_AUTH_ANONYMOUS_ENABLED" in text:
        return "compose: уже настроено"
    anchor = "      - GF_USERS_ALLOW_SIGN_UP=false\n"
    if anchor not in text:
        return "compose: не нашёл блок Grafana (GF_USERS_ALLOW_SIGN_UP)"
    backup(compose)
    compose.write_text(text.replace(anchor, anchor + GRAFANA_ENV, 1), encoding="utf-8")
    return "compose: переменные Grafana добавлены"


def main() -> None:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/home/yartsevn/kag-system")
    print(patch_nginx(root))
    print(patch_compose(root))


if __name__ == "__main__":
    main()
