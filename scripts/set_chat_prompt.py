"""Обновить системный промпт функции из локального файла (например prompts/chat.txt).

Зачем: живой промпт чата хранится в настройках (config_store → function_map/chat),
а файл prompts/<функция>.txt — только значение по умолчанию, которое берётся,
когда в настройках пусто. Поэтому правка файла сама по себе ничего не меняет:
его нужно загрузить в настройки — этот скрипт и делает.

Запуск на сервере (пароль берётся из окружения, не из аргументов):

    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    python3 scripts/set_chat_prompt.py prompts/chat.txt
    python3 scripts/set_chat_prompt.py prompts/chat.txt --function chat --dry-run

Что делает:
1. логинится под admin (POST /api/v1/auth/login);
2. читает текущую привязку функции (GET /api/v1/admin/models/functions/<fn>) —
   чтобы НЕ потерять provider_id, model и parameters;
3. подменяет только system_prompt на содержимое файла и сохраняет
   (POST /api/v1/admin/models/functions);
4. печатает, сколько символов было и стало, и первое расхождение — чтобы видеть,
   что подменилось именно то, что нужно.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _post(url: str, payload: dict, token: str | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt_file", help="путь к файлу промпта, например prompts/chat.txt")
    ap.add_argument("--function", default="chat", help="имя функции (по умолчанию chat)")
    ap.add_argument("--api", default=os.environ.get("KAG_API", "http://localhost:8000"))
    ap.add_argument("--user", default=os.environ.get("KAG_USER", "admin"))
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет отправлено")
    args = ap.parse_args()

    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        print("нет ADMIN_PASSWORD в окружении (set -a; . ./.env; set +a)", file=sys.stderr)
        return 2

    new_prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    api = args.api.rstrip("/")

    token = _post(f"{api}/api/v1/auth/login",
                  {"username": args.user, "password": password}).get("access_token")
    if not token:
        print("не удалось получить токен", file=sys.stderr)
        return 3

    current = _get(f"{api}/api/v1/admin/models/functions/{args.function}", token)
    old_prompt = current.get("system_prompt") or ""
    print(f"функция: {args.function} | провайдер: {current.get('provider_id')} | модель: {current.get('model')}")
    print(f"было символов: {len(old_prompt)} | станет: {len(new_prompt)}")

    if old_prompt == new_prompt:
        print("промпт уже совпадает с файлом — ничего не меняю")
        return 0

    # первое расхождение — быстрый признак, что подменяется то, что нужно
    for i, (a, b) in enumerate(zip(old_prompt, new_prompt)):
        if a != b:
            print(f"первое расхождение на позиции {i}: ...{old_prompt[max(0, i - 40):i + 40]!r} → ...{new_prompt[max(0, i - 40):i + 40]!r}")
            break

    payload = {
        "function": args.function,
        "provider_id": current.get("provider_id") or "",
        "model": current.get("model") or "",
        "system_prompt": new_prompt,
        "parameters": current.get("parameters") or {"temperature": 0.7, "max_tokens": 4096},
    }
    if args.dry_run:
        print("dry-run: отправка пропущена")
        return 0

    try:
        result = _post(f"{api}/api/v1/admin/models/functions", payload, token)
    except urllib.error.HTTPError as e:
        print(f"ошибка сохранения: {e.code} {e.read().decode('utf-8', 'replace')[:300]}", file=sys.stderr)
        return 4

    saved = _get(f"{api}/api/v1/admin/models/functions/{args.function}", token).get("system_prompt") or ""
    ok = saved == new_prompt
    print(f"сохранено: {'да' if ok else 'НЕТ'} | в настройках теперь {len(saved)} символов")
    print(f"модель после сохранения: {_get(f'{api}/api/v1/admin/models/functions/{args.function}', token).get('model')}")
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
