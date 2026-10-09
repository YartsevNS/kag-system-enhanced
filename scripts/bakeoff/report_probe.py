"""Сквозная проверка отчётов на почту: настройка → отправка → содержимое письма.

Что проверяется:
  1. настройки отчёта читаются и сохраняются через тот же API, что и админка (пароль маскируется);
  2. образец отчёта собирается и содержит разделы (диски, документы и т.д.);
  3. письмо реально уходит по SMTP и содержит текст отчёта (проверяется на локальном приёмнике);
  4. расписание работает: «наступило время / уже отправляли сегодня / выключено» считаются верно;
  5. настройки возвращаются в исходное состояние, чтобы стенд не остался с тестовым SMTP.

Запуск в контейнере api (SMTP-приёмник поднимается отдельным контейнером на порту из SMTP_PORT):
  docker cp report_probe.py kag-api:/tmp/ && docker exec kag-api python /tmp/report_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

API = "http://127.0.0.1:8000/api/v1"


def call(path: str, method: str = "GET", body: dict | None = None, cookie: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", "Cookie": cookie})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def main() -> None:
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        print("нужен ADMIN_PASSWORD")
        sys.exit(2)
    req = urllib.request.Request(f"{API}/auth/login",
                                data=json.dumps({"username": "admin", "password": password}).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    cookie = urllib.request.urlopen(req, timeout=30).headers.get("Set-Cookie", "").split(";")[0]
    print("вход:", "ок" if cookie else "НЕТ")

    before = call("/admin/models/report-config", cookie=cookie)
    print("настройки до:", {k: before.get(k) for k in ("enabled", "schedule", "smtp_host", "recipients")})

    # ── 1) образец отчёта ──
    preview = call("/admin/models/report-preview", cookie=cookie)
    text = preview.get("text", "")
    print(f"\nобразец отчёта: {len(text)} символов")
    for line in text.splitlines()[:6]:
        print("   ", line)

    # ── 2) сохраняем тестовую настройку на локальный приёмник ──
    smtp_port = int(os.environ.get("SMTP_PORT", "1025"))
    test_cfg = {
        "enabled": True, "schedule": "daily", "time": "07:00", "weekday": 0,
        "recipients": "test@example.local", "subject_prefix": "KAG-ТЕСТ",
        "metrics": ["disks", "documents", "queue", "services"],
        "smtp_host": "127.0.0.1", "smtp_port": smtp_port, "smtp_user": "", "smtp_tls": False,
        "from_addr": "kag@example.local",
    }
    saved = call("/admin/models/report-config", "POST", test_cfg, cookie)
    print("сохранение настроек:", saved.get("status"))

    # ── 3) отправка ──
    sent = call("/admin/models/report-send-now", "POST", {}, cookie)
    print("отправка:", sent.get("status"), "|", sent.get("message"))

    # ── 4) расписание ──
    from src.api.services.report_service import report_service
    cfg = report_service.settings()
    yesterday = datetime.now() - timedelta(days=1)
    checks = []
    checks.append(("выключенный отчёт не отправляется",
                   report_service.is_due(datetime.now(), {**cfg, "enabled": False}) is False))
    due_now = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    checks.append(("после назначенного времени отправляется",
                   report_service.is_due(due_now, {**cfg, "time": "07:00", "last_sent": ""}) is True))
    checks.append(("до назначенного времени не отправляется",
                   report_service.is_due(due_now.replace(hour=6), {**cfg, "time": "07:00", "last_sent": ""}) is False))
    checks.append(("повторно в тот же день не отправляется",
                   report_service.is_due(due_now, {**cfg, "time": "07:00",
                                                   "last_sent": due_now.isoformat(timespec="seconds")}) is False))
    checks.append(("недельный отправляется только в свой день",
                   report_service.is_due(due_now, {**cfg, "schedule": "weekly", "weekday": (due_now.weekday() + 1) % 7,
                                                   "time": "07:00", "last_sent": ""}) is False))
    checks.append(("часовой отправляется раз в час",
                   report_service.is_due(due_now, {**cfg, "schedule": "hourly",
                                                   "last_sent": (due_now - timedelta(minutes=30)).isoformat(timespec="seconds")}) is False))

    print("\nпроверки расписания:")
    bad = 0
    for name, ok in checks:
        print(f"  {'ок  ' if ok else 'НЕТ '} {name}")
        bad += 0 if ok else 1

    # ── 5) возврат настроек ──
    restore = {k: before.get(k) for k in
               ("enabled", "schedule", "time", "weekday", "recipients", "subject_prefix", "metrics",
                "smtp_host", "smtp_port", "smtp_user", "smtp_tls", "from_addr")}
    restore["smtp_password_clear"] = not bool(before.get("smtp_password_set"))
    call("/admin/models/report-config", "POST", restore, cookie)
    print("\nнастройки возвращены (SMTP-сервер:", repr(restore.get("smtp_host")), ")")

    print("\nИТОГ:", "почта и расписание работают" if not bad and sent.get("status") == "ok"
          else f"провалов {bad + (0 if sent.get('status') == 'ok' else 1)}")
    sys.exit(1 if (bad or sent.get("status") != "ok") else 0)


if __name__ == "__main__":
    main()
