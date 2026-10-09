"""Закрыть служебные порты от внешней сети: оставить только страницы проекта.

Правило владельца: снаружи, из интернета, доступ должен быть закрыт, за исключением страниц проекта,
и только после авторизации. Вход в проект — через nginx (80/443, за реверс-прокси заказчика);
всё остальное (Qdrant, Redis, Prometheus, Neo4j, Keycloak, MCP) наружу светить не должно.

Что делает скрипт:
  1. В дереве развёртывания у перечисленных сервисов меняет публикацию портов на привязку к 127.0.0.1
     (снаружи недоступно, с самого стенда и через ssh-туннель — доступно для отладки).
     api и nginx не трогает: api уже на 127.0.0.1, nginx — точка входа проекта.
  2. Пересоздаёт затронутые сервисы по одному, с проверкой состояния после каждого.
  3. Печатает итог: что осталось опубликовано.

Идемпотентно, с резервными копиями рядом (docker-compose.yml.bak-*). Запуск на стенде:
  python3 apply_network_hardening.py /home/yartsevn/kag-system
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

# сервис -> порты, которые надо убрать с внешних интерфейсов
SERVICES = {
    "qdrant": ["6333", "6334"],
    "redis": ["6379"],
    "prometheus": ["9090"],
    "mcp-server": ["8001"],
    "keycloak": ["8080", "8443"],
    "neo4j": ["7474", "7687", "7473"],
}


def backup(path: pathlib.Path) -> pathlib.Path:
    dst = path.with_suffix(path.suffix + f".bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, dst)
    return dst


def harden_ports(compose: pathlib.Path) -> list[str]:
    """Привязать публикации портов перечисленных сервисов к 127.0.0.1."""
    text = compose.read_text(encoding="utf-8")
    original = text
    notes: list[str] = []

    for service, ports in SERVICES.items():
        # найдём блок сервиса
        m = re.search(rf"\n  {re.escape(service)}:\n(.*?)(?=\n  \w|\Z)", text, re.S)
        if not m:
            notes.append(f"{service}: сервис не найден")
            continue
        block = m.group(0)
        new_block = block
        for port in ports:
            # варианты записи: "6333:6333", "6333-6334:6333-6334", "8080:8080"
            for pattern, repl in (
                (rf'      - "0\.0\.0\.0:{port}:{port}"', f'      - "127.0.0.1:{port}:{port}"'),
                (rf'      - "{port}:{port}"', f'      - "127.0.0.1:{port}:{port}"'),
                (rf'      - "{port}-(\d+):{port}-\1"', f'      - "127.0.0.1:{port}-\\1:{port}-\\1"'),
            ):
                new_block, n = re.subn(pattern, repl, new_block)
                if n:
                    notes.append(f"{service}: порт {port} закрыт снаружи")
            new_block = new_block.replace(f'      - "::: {port}"', "")
        if new_block != block:
            text = text.replace(block, new_block)

    if text != original:
        backup(compose)
        compose.write_text(text, encoding="utf-8")
    return notes


def compose(*args: str, cwd: pathlib.Path) -> str:
    res = subprocess.run(["docker-compose", *args], cwd=str(cwd), capture_output=True, text=True)
    return (res.stdout + res.stderr).strip()


def main() -> None:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/home/yartsevn/kag-system")
    compose_file = root / "docker-compose.yml"
    if not compose_file.exists():
        print("нет docker-compose.yml в", root)
        return

    print("=== правка публикации портов ===")
    for note in harden_ports(compose_file):
        print(" -", note)

    print("\n=== проверка, что compose разбирается ===")
    # Проверяем КОД ВОЗВРАТА, а не текст: `config -q` молчит при успехе, а вывод обычного `config`
    # содержит слова «error»/«yaml» в описаниях сервисов — на этом скрипт однажды зря остановился
    # уже после правки файла (порты переписаны, контейнеры не пересозданы).
    res = subprocess.run(["docker-compose", "config", "-q"], cwd=str(root),
                         capture_output=True, text=True)
    if res.returncode != 0:
        print("ОШИБКА разбора compose, дальше не иду:\n", (res.stderr or res.stdout)[:400])
        return
    print("ok")

    print("\n=== пересоздаю сервисы по одному с проверкой ===")
    for service in ("prometheus", "mcp-server", "keycloak", "qdrant", "redis", "neo4j"):
        print(f"\n--- {service} ---")
        print(compose("up", "-d", "--force-recreate", "--no-deps", service, cwd=root)[-300:])
        time.sleep(8)
        name = compose("ps", "-q", service, cwd=root).strip()
        if not name:
            print(f"{service}: контейнер не найден — проверьте вручную")
            continue
        state = subprocess.run(["docker", "inspect", "-f",
                                "{{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}—{{end}}",
                                name], capture_output=True, text=True).stdout.strip()
        print(f"{service}: {state}")

    print("\n=== что осталось опубликовано наружу ===")
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        name, _, ports = line.partition("|")
        external = [p for p in ports.split(",") if p.strip() and "127.0.0.1" not in p]
        if external:
            print(f"  {name}: {', '.join(p.strip() for p in external)}")

    print("\n=== приложение живо? ===")
    print(compose("exec", "-T", "api", "curl", "-s", "-o", "/dev/null", "-w", "health: %{http_code}",
                  "http://127.0.0.1:8000/api/v1/health", cwd=root))
    subprocess.run(["curl", "-sk", "-o", "/dev/null", "-w", "SSO через nginx: %{http_code}\n",
                    "https://127.0.0.1/realms/kag/.well-known/openid-configuration"])


if __name__ == "__main__":
    main()
