"""Прибор выката: по изменённым файлам говорит, какие ОБРАЗЫ надо пересобрать.

Зачем. Уже дважды теряли время на одном и том же: правку выкатывали только в api, а код исполняет
и воркер (построение графа, конвейер обработки) — и пересборка корпуса шла старым кодом.
Цена: 55 минут работы стенда и 1528 связей без происхождения. Прибор делает проверку обязательной.

Как пользоваться (в рабочей копии, перед сборкой):
    python scripts/bakeoff/deploy_check.py --deployed 2026.10.10.17
    python scripts/bakeoff/deploy_check.py --deployed 2026.10.10.17 --json   # машинный вывод

Что проверяет:
  1) какие файлы изменились с момента последнего выката (по git, включая незакоммиченные правки);
  2) какие образы из этого следуют: api, worker, base (или ни один);
  3) что воркер не забыт, если правка касается кода, который он исполняет;
  4) напоминает про проверку кода ВНУТРИ контейнера (health зелёный, а код может быть старый).
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict

# Что где исполняется. Правило простое: src/** исполняют ОБА сервиса (api и оба воркера —
# worker и worker-maintenance, один образ), src/api/** — только api, статика — только api.
RULES = [
    ("docker/base/", {"base", "api", "worker"}, "базовый образ: пересобрать ПЕРВЫМ, потом тонкие"),
    ("requirements", {"api", "worker"}, "зависимости Python: оба сервиса"),
    ("Dockerfile.worker", {"worker"}, "описание образа воркера"),
    ("Dockerfile", {"api"}, "описание образа api"),
    ("src/indexing/", {"api", "worker"}, "конвейер/граф исполняет ВОРКЕР — его забывают чаще всего"),
    ("src/security/", {"api", "worker"}, "журналы и защита используются обоими"),
    ("src/database/", {"api", "worker"}, "миграции применяет api, но читает и воркер"),
    ("src/config.py", {"api", "worker"}, "настройки читают оба"),
    ("src/models.py", {"api", "worker"}, "общие модели"),
    ("src/api/", {"api"}, "веб-слой: только api"),
    ("src/", {"api", "worker"}, "прочее в src — по умолчанию оба"),
    ("scripts/", set(), "инструмент: образ не нужен, но при запуске ВНУТРИ контейнера файл надо скопировать"),
    ("docs/", set(), "документация: образ не нужен"),
]


def resolve_ref(ref: str) -> str:
    """Проверить, что ссылка существует. Отказ должен быть громким: иначе прибор молча скажет
    «пересобирать нечего» и выкат пройдёт вслепую — это и есть та ошибка, из-за которой мы теряем время."""
    r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tags = subprocess.run(["git", "tag", "--sort=-creatordate"], capture_output=True, text=True).stdout.split()
        print(f"ОШИБКА: ссылка «{ref}» не найдена в git. Нужен коммит или ТЕГ git, а не тег образа Docker.",
              file=sys.stderr)
        print(f"      последние теги git: {', '.join(tags[:5]) or '(нет)'}", file=sys.stderr)
        print("      пример: --deployed 68aa5f6  либо  --deployed stable-vocab-v0-2026.10.10", file=sys.stderr)
        raise SystemExit(2)
    return r.stdout.strip()


def changed_files(deployed: str) -> list[str]:
    """Файлы, изменённые с момента выката: коммиты после него плюс незакоммиченные правки."""
    out: list[str] = []
    r = subprocess.run(["git", "diff", "--name-only", f"{deployed}..HEAD"],
                       capture_output=True, text=True, check=True)
    out += [l for l in r.stdout.splitlines() if l.strip()]
    # незакоммиченное — иначе правка в рабочей копии не попадёт в план
    for args in (["git", "diff", "--name-only"], ["git", "diff", "--name-only", "--cached"]):
        r = subprocess.run(args, capture_output=True, text=True)
        out += [l for l in r.stdout.splitlines() if l.strip()]
    return sorted(set(out))


def plan(files: list[str]) -> dict:
    need: dict[str, list[str]] = defaultdict(list)
    notes: dict[str, str] = {}
    for f in files:
        for prefix, images, note in RULES:
            if f.startswith(prefix):
                for img in images:
                    need[img].append(f)
                if images and prefix not in notes:
                    notes[prefix] = note
                break
    return {"images": {k: sorted(set(v)) for k, v in need.items()}, "notes": notes}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deployed", required=True, help="тег/коммит последнего выката, например 2026.10.10.17")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    ref = resolve_ref(a.deployed)
    files = changed_files(ref)
    print(f"сравниваю с {a.deployed} ({ref[:8]})")
    p = plan(files)
    images = p["images"]

    if a.json:
        print(json.dumps({"changed": len(files), **p}, ensure_ascii=False, indent=2))
        return 0

    print(f"изменено файлов с выката {a.deployed}: {len(files)}")
    if not files:
        print("\nобразы пересобирать не нужно.")
        return 0

    order = [i for i in ("base", "worker", "api") if i in images]
    print(f"\nПЕРЕСОБРАТЬ ОБРАЗЫ: {', '.join(order) if order else 'никакие'}")
    for img in order:
        tag = {"base": "kre44et/kag-base", "api": "kre44et/kag-api", "worker": "kre44et/kag-worker"}[img]
        df = {"base": "-f docker/base/Dockerfile .", "api": "-f Dockerfile .", "worker": "-f Dockerfile.worker ."}[img]
        print(f"   docker build -t {tag}:<НОВЫЙ_ТЕГ> {df}")
        if img == "worker":
            print("   ОБА сервиса на одном образе: worker И worker-maintenance — пересоздавать оба")
    if "base" in images:
        print("   ВНИМАНИЕ: после базового образа обновить FROM в тонких Dockerfile и собрать их заново")
    if "worker" in images and "api" not in images:
        print("   ВНИМАНИЕ: правка касается кода воркера — выкат только api её НЕ применит")
    if "api" in images and "worker" not in images:
        print("   (правка только веб-слоя: воркер пересобирать не нужно)")

    print("\nДальше обязательно:")
    print("   1) теги образов в docker-compose.yml заменить на новые;")
    print("   2) docker-compose up -d --force-recreate <api|worker|worker-maintenance>;")
    print("   3) health вернулся? НЕТ — этого мало: health зелёный и на старом коде.")
    print("      Проверить КОД внутри контейнера, например:")
    for img in order:
        if img in ("api", "worker"):
            svc = "kag-api" if img == "api" else "kag-system_worker-maintenance_1"
            print(f"      docker exec {svc} grep -c '<строка_из_правки>' /app/<файл_правки>")
    print("   4) после выката — обновить этот же чек: --deployed <НОВЫЙ_ТЕГ>.")

    # Показать, какие именно файлы привели к воркеру: чаще всего это и есть забытая правка
    if "worker" in images:
        print("\nфайлы, из-за которых нужен воркер:")
        for f in images["worker"][:12]:
            print(f"   {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
