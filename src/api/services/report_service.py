"""Отчёты на почту: список показателей, сбор, рендер письма и отправка по расписанию.

Зачем: заказчику нужен регулярный отчёт о состоянии системы (место на дисках, документы, очередь,
доступность сервисов) на почту, без захода в интерфейс. Отчёт собирается из тех же данных, что видны
в админке, и отправляется по SMTP.

Устройство:
  * METRICS — список показателей; какие включать, выбирает администратор в админке;
  * collect(codes) — сбор; каждая группа собирается независимо и при сбое не ломает остальные
    (в отчёт попадает пометка «ошибка»);
  * render_text / render_html — письмо двумя видами: текстовый (читается везде, в том числе в
    корпоративной почте без HTML) и HTML;
  * send() — SMTP с необязательным STARTTLS; пароль берётся из настроек и НИКОГДА не пишется в лог;
  * run_due() — вызывается планировщиком раз в минуту: если наступило время, отправляет и запоминает
    факт отправки (last_sent), чтобы не дублировать.

Настройки живут в config_store, ключ `system`/`reports` (рядом с брендингом и загрузкой), и правятся
в админке: раздел «Отчёты на почту». Пароль в ответах API маскируется.
"""

from __future__ import annotations

import asyncio
import json
import os
import smtplib
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

# ── Показатели отчёта ────────────────────────────────────────────────────────
# (код, название, показывать по умолчанию)
METRICS: List[Tuple[str, str, bool]] = [
    ("disks", "Диски и файловые системы", True),
    ("docker", "Docker: образы, контейнеры, тома", True),
    ("documents", "Документы по состояниям", True),
    ("corpus", "Корпус: векторы, граф, таблицы, вес данных", True),
    ("services", "Доступность сервисов", True),
    ("queue", "Очередь обработки и ошибки", True),
]
METRIC_CODES = [m[0] for m in METRICS]
METRIC_TITLES = {m[0]: m[1] for m in METRICS}

SCHEDULES = ("hourly", "daily", "weekly")
DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "schedule": "daily",
    "time": "08:30",                 # для daily и weekly
    "weekday": 0,                    # 0 = понедельник (для weekly)
    "recipients": "",
    "subject_prefix": "KAG",
    "metrics": ["disks", "documents", "queue", "services"],
    "smtp_host": "",
    "smtp_port": 25,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_tls": False,
    "from_addr": "",
    "last_sent": "",
}

# пороги «внимание/критично» для дисков — используются и в отчёте, и в подписи письма
DISK_WARN = 80.0
DISK_CRIT = 90.0


class ReportService:
    """Сбор показателей и отправка отчёта на почту."""

    # ── настройки ────────────────────────────────────────────────────────────
    @staticmethod
    def settings() -> Dict[str, Any]:
        cfg = dict(DEFAULTS)
        try:
            from src.api.services.config_store import config_store
            raw = config_store.get("system", "reports") or {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in cfg:
                        cfg[k] = v
                if not isinstance(cfg.get("metrics"), list):
                    cfg["metrics"] = list(DEFAULTS["metrics"])
        except Exception as e:
            logger.debug(f"настройки отчётов не прочитаны: {e}")
        return cfg

    @staticmethod
    def public_settings() -> Dict[str, Any]:
        """Настройки для админки: пароль не отдаём, только признак «задан»."""
        cfg = ReportService.settings()
        cfg["smtp_password_set"] = bool(cfg.get("smtp_password"))
        cfg["smtp_password"] = ""
        return cfg

    @staticmethod
    def save_settings(data: Dict[str, Any]) -> Dict[str, Any]:
        """Сохранить настройки с проверкой значений (браузеру веры нет)."""
        from src.api.services.config_store import config_store

        cfg = ReportService.settings()

        if "enabled" in data:
            cfg["enabled"] = bool(data["enabled"])
        if "schedule" in data:
            cfg["schedule"] = data["schedule"] if data["schedule"] in SCHEDULES else "daily"
        if "time" in data:
            v = str(data["time"] or "").strip()
            try:
                hh, mm = v.split(":")
                cfg["time"] = f"{max(0, min(23, int(hh))):02d}:{max(0, min(59, int(mm))):02d}"
            except (ValueError, AttributeError):
                cfg["time"] = DEFAULTS["time"]
        if "weekday" in data:
            try:
                cfg["weekday"] = max(0, min(6, int(data["weekday"])))
            except (TypeError, ValueError):
                cfg["weekday"] = 0
        if "recipients" in data:
            cfg["recipients"] = str(data["recipients"] or "").strip()[:500]
        if "subject_prefix" in data:
            cfg["subject_prefix"] = str(data["subject_prefix"] or "KAG").strip()[:60] or "KAG"
        if "metrics" in data and isinstance(data["metrics"], list):
            cfg["metrics"] = [m for m in data["metrics"] if m in METRIC_CODES] or list(DEFAULTS["metrics"])
        for key in ("smtp_host", "smtp_user", "from_addr"):
            if key in data:
                cfg[key] = str(data[key] or "").strip()[:200]
        if "smtp_port" in data:
            try:
                cfg["smtp_port"] = max(1, min(65535, int(data["smtp_port"])))
            except (TypeError, ValueError):
                cfg["smtp_port"] = 25
        if "smtp_tls" in data:
            cfg["smtp_tls"] = bool(data["smtp_tls"])
        # Пароль меняем только если прислали непустое значение: пустое поле в форме означает
        # «оставить как было», иначе пароль стирался бы при каждом сохранении.
        if data.get("smtp_password"):
            cfg["smtp_password"] = str(data["smtp_password"])[:200]
        if data.get("smtp_password_clear"):
            cfg["smtp_password"] = ""

        config_store.set("system", "reports", cfg)
        return cfg

    # ── сбор показателей ─────────────────────────────────────────────────────
    def collect(self, codes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        codes = [c for c in (codes or list(METRIC_CODES)) if c in METRIC_CODES]
        out: List[Dict[str, Any]] = []
        for code in codes:
            title = METRIC_TITLES.get(code, code)
            item: Dict[str, Any] = {"code": code, "title": title, "rows": [], "warn": False, "error": ""}
            handler: Callable[[Dict[str, Any]], None] = getattr(self, f"_metric_{code}", None)  # type: ignore[assignment]
            if handler is None:
                item["error"] = "показатель не реализован"
                out.append(item)
                continue
            try:
                handler(item)
            except Exception as e:
                item["error"] = f"{type(e).__name__}: {e}"
                logger.warning(f"Отчёт: показатель {code} не собрался: {e}")
            out.append(item)
        return out

    # диски ───────────────────────────────────────────────────────────────────
    def _metric_disks(self, item: Dict[str, Any]) -> None:
        import psutil

        seen = set()
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            key = (part.device, part.mountpoint)
            if key in seen or usage.total < 100 * 1024 * 1024:
                continue
            seen.add(key)
            percent = float(usage.percent)
            mark = "критично" if percent >= DISK_CRIT else ("внимание" if percent >= DISK_WARN else "")
            item["rows"].append((
                f"{part.mountpoint} ({part.fstype})",
                f"{_gb(usage.used)} из {_gb(usage.total)}, свободно {_gb(usage.free)} "
                f"— {percent:.0f}%{(' — ' + mark) if mark else ''}",
            ))
            if percent >= DISK_CRIT:
                item["warn"] = True
        if not item["rows"]:
            item["rows"].append(("файловые системы", "не удалось прочитать"))

    # docker ──────────────────────────────────────────────────────────────────
    def _metric_docker(self, item: Dict[str, Any]) -> None:
        try:
            from src.api.services.docker_monitor import docker_monitor
            info = docker_monitor.get_system_info() or {}
            if info.get("images") is not None:
                item["rows"].append(("образы", str(info.get("images"))))
            if info.get("containers") is not None:
                item["rows"].append(("контейнеры", str(info.get("containers"))))
            if info.get("containers_running") is not None:
                item["rows"].append(("запущено", str(info.get("containers_running"))))
        except Exception as e:
            logger.debug(f"Отчёт: docker SDK недоступен ({e}) — считаю через CLI/сокет")

        # Переиспользуемое место: важно, потому что старые образы копятся (у нас 16 ГБ из 18 ГБ)
        out = _run(["docker", "system", "df", "--format", "{{.Type}}|{{.Size}}|{{.Reclaimable}}"])
        if out:
            for line in out.splitlines():
                parts = line.split("|")
                if len(parts) == 3:
                    item["rows"].append((f"{parts[0]}: всего", f"{parts[1]}, можно освободить {parts[2]}"))
        # Размер каталога docker (если виден)
        for path in ("/var/lib/docker", "/var/lib/docker/overlay2"):
            if os.path.isdir(path):
                try:
                    total = _du(path)
                    item["rows"].append((f"каталог {path}", _gb(total)))
                except Exception:
                    pass
                break

    # документы ───────────────────────────────────────────────────────────────
    def _metric_documents(self, item: Dict[str, Any]) -> None:
        rows = _sql("select status, count(*) from documents group by status order by 2 desc")
        total = 0
        for status, count in rows:
            item["rows"].append((f"состояние «{status}»", str(count)))
            total += int(count or 0)
        item["rows"].append(("всего документов", str(total)))
        day = _sql("select count(*) from documents where created_at > now() - interval '1 day'")
        if day:
            item["rows"].append(("загружено за сутки", str(day[0][0])))
        if any(str(s) == "failed" and int(c or 0) > 0 for s, c in rows):
            item["warn"] = True

    # корпус ──────────────────────────────────────────────────────────────────
    def _metric_corpus(self, item: Dict[str, Any]) -> None:
        tables = _sql("select count(*) from document_tables")
        if tables:
            item["rows"].append(("таблиц распознано", str(tables[0][0])))
        rows_tbl = _sql("select count(*) from table_rows")
        if rows_tbl:
            item["rows"].append(("строк в таблицах", str(rows_tbl[0][0])))

        # Qdrant: число точек (векторов)
        try:
            from src.indexing.qdrant_service import settings as _settings  # type: ignore
            base = f"http://{getattr(_settings, 'QDRANT_HOST', 'kag-qdrant')}:{getattr(_settings, 'QDRANT_PORT', 6333)}"
        except Exception:
            base = "http://kag-qdrant:6333"
        data = _http_json(f"{base}/collections/kag_documents")
        points = (((data or {}).get("result") or {}).get("points_count"))
        if points is not None:
            item["rows"].append(("векторов в Qdrant", str(points)))

        # Neo4j: узлы графа (HTTP transactional API, пароль из окружения)
        nodes = _neo4j_count()
        if nodes is not None:
            item["rows"].append(("узлов в графе", str(nodes)))

        # Вес данных на диске
        for path, label in ((os.environ.get("DATA_DIR", "/app/data") + "/uploads", "файлы документов"),
                            (os.environ.get("DATA_DIR", "/app/data") + "/thumbnails", "миниатюры"),
                            (os.environ.get("DATA_DIR", "/app/data") + "/ocr_results", "результаты распознавания")):
            if os.path.isdir(path):
                try:
                    item["rows"].append((label, _gb(_du(path))))
                except Exception:
                    pass

    # сервисы ─────────────────────────────────────────────────────────────────
    def _metric_services(self, item: Dict[str, Any]) -> None:
        checks: List[Tuple[str, Callable[[], Tuple[bool, str]]]] = [
            ("PostgreSQL", lambda: _tcp(os.environ.get("POSTGRES_HOST", "kag-postgres"),
                                       int(os.environ.get("POSTGRES_PORT", 5432)))),
            ("Redis", lambda: _redis_ping(os.environ.get("REDIS_HOST", "kag-redis"),
                                          int(os.environ.get("REDIS_PORT", 6379)))),
            ("Qdrant", lambda: _http_ok(os.environ.get("QDRANT_URL", "http://kag-qdrant:6333/healthz"))),
            ("Neo4j", lambda: _tcp("kag-neo4j", 7687)),
        ]
        try:
            from src.indexing import ocr_client
            cfg = ocr_client.get_service_config() or {}
            url = str(cfg.get("url") or "")
            if url:
                checks.append((f"Служба OCR ({url})", lambda u=url: _http_ok(u.rstrip("/") + "/health")))
        except Exception:
            pass

        for name, fn in checks:
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}"
            item["rows"].append((name, ("доступен" if ok else "НЕ отвечает") + (f" ({detail})" if detail else "")))
            if not ok:
                item["warn"] = True

    # очередь ─────────────────────────────────────────────────────────────────
    def _metric_queue(self, item: Dict[str, Any]) -> None:
        rows = _sql("select status, count(*) from documents "
                    "where status in ('pending','processing','failed') group by status")
        found = False
        for status, count in rows:
            item["rows"].append((f"«{status}»", str(count)))
            found = True
            if str(status) == "failed" and int(count or 0) > 0:
                item["warn"] = True
        if not found:
            item["rows"].append(("нет документов в очереди и с ошибками", "0"))
        done = _sql("select count(*) from documents where status = 'completed' "
                    "and updated_at > now() - interval '1 day'")
        if done:
            item["rows"].append(("обработано за сутки", str(done[0][0])))
        last_err = _sql("select filename, updated_at from documents where status = 'failed' "
                        "order by updated_at desc limit 3")
        for name, when in last_err or []:
            item["rows"].append((f"последняя ошибка: {str(name)[:40]}", str(when)[:19]))

    # ── рендер ────────────────────────────────────────────────────────────────
    def render_text(self, sections: List[Dict[str, Any]], when: Optional[datetime] = None) -> str:
        when = when or datetime.now()
        lines = [f"Отчёт KAG за {when.strftime('%d.%m.%Y %H:%M')}", "=" * 60, ""]
        warn = [s["title"] for s in sections if s.get("warn") or s.get("error")]
        if warn:
            lines += [f"Требует внимания: {', '.join(warn)}", ""]
        for sec in sections:
            lines.append(f"— {sec['title']}")
            if sec.get("error"):
                lines.append(f"    не собрано: {sec['error']}")
            for label, value in sec["rows"]:
                lines.append(f"    {label}: {value}")
            lines.append("")
        lines.append("Отчёт собран автоматически. Настройка — админка, раздел «Отчёты на почту».")
        return "\n".join(lines)

    def render_html(self, sections: List[Dict[str, Any]], when: Optional[datetime] = None) -> str:
        when = when or datetime.now()
        warn = [s["title"] for s in sections if s.get("warn") or s.get("error")]
        css = ("font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#12161b;"
               "line-height:1.5;font-size:14px")
        parts = [f"<div style='{css}'>",
                 f"<h2 style='margin:0 0 4px'>Отчёт KAG</h2>",
                 f"<div style='color:#55606d;margin-bottom:12px'>{when.strftime('%d.%m.%Y %H:%M')}</div>"]
        if warn:
            parts.append("<div style='background:#fff4e5;border:1px solid #e0a800;border-radius:8px;"
                         f"padding:10px 12px;margin-bottom:14px'>Требует внимания: {', '.join(warn)}</div>")
        for sec in sections:
            parts.append(f"<h3 style='margin:18px 0 6px;font-size:15px'>{sec['title']}</h3>")
            if sec.get("error"):
                parts.append(f"<div style='color:#b3261e'>не собрано: {sec['error']}</div>")
                continue
            parts.append("<table cellspacing='0' cellpadding='6' style='border-collapse:collapse;width:100%;"
                         "background:#ffffff;border:1px solid #e3e6ea;border-radius:8px'>")
            for label, value in sec["rows"]:
                parts.append("<tr>"
                             f"<td style='border-bottom:1px solid #eceff3;color:#3e464f'>{label}</td>"
                             f"<td style='border-bottom:1px solid #eceff3;text-align:right;white-space:nowrap'>{value}</td>"
                             "</tr>")
            parts.append("</table>")
        parts.append("<p style='color:#6e7a88;font-size:12px;margin-top:16px'>Отчёт собран автоматически. "
                     "Настройка — админка, раздел «Отчёты на почту».</p></div>")
        return "".join(parts)

    # ── отправка ──────────────────────────────────────────────────────────────
    def send(self, text: str, html: str = "", subject: str = "", settings: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        cfg = settings or self.settings()
        host = str(cfg.get("smtp_host") or "").strip()
        recipients = [r.strip() for r in str(cfg.get("recipients") or "").replace(";", ",").split(",") if r.strip()]
        if not host:
            return False, "не задан SMTP-сервер (админка → Отчёты на почту)"
        if not recipients:
            return False, "не заданы получатели"

        from_addr = str(cfg.get("from_addr") or cfg.get("smtp_user") or "kag@localhost").strip()
        prefix = str(cfg.get("subject_prefix") or "KAG").strip()
        stamp = datetime.now().strftime("%d.%m.%Y")
        subject = subject or f"{prefix}: отчёт за {stamp}"

        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")

        port = int(cfg.get("smtp_port") or 25)
        try:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if cfg.get("smtp_tls"):
                    smtp.starttls()
                user = str(cfg.get("smtp_user") or "")
                password = str(cfg.get("smtp_password") or "")
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
            logger.info(f"Отчёт отправлен на {len(recipients)} адрес(ов) через {host}:{port}")
            return True, f"отправлено на {', '.join(recipients)}"
        except Exception as e:
            logger.warning(f"Отчёт не удалось отправить через {host}:{port}: {type(e).__name__}: {e}")
            return False, f"{type(e).__name__}: {e}"

    # ── расписание ────────────────────────────────────────────────────────────
    def is_due(self, now: Optional[datetime] = None, cfg: Optional[Dict[str, Any]] = None) -> bool:
        cfg = cfg or self.settings()
        if not cfg.get("enabled"):
            return False
        now = now or datetime.now()
        last = _parse_dt(str(cfg.get("last_sent") or ""))

        if cfg.get("schedule") == "hourly":
            return last is None or now - last >= timedelta(hours=1)

        try:
            hh, mm = (int(x) for x in str(cfg.get("time") or "08:30").split(":"))
        except (ValueError, TypeError):
            hh, mm = 8, 30
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < target:
            return False
        if last and last >= target:
            return False                                    # уже отправляли сегодня
        if cfg.get("schedule") == "weekly":
            return now.weekday() == int(cfg.get("weekday") or 0)
        return True

    def run_due(self, now: Optional[datetime] = None) -> Optional[Tuple[bool, str]]:
        """Отправить отчёт, если пришло время. Возвращает (успех, сообщение) или None."""
        cfg = self.settings()
        if not self.is_due(now, cfg):
            return None
        sections = self.collect(list(cfg.get("metrics") or METRIC_CODES))
        text = self.render_text(sections, now)
        ok, message = self.send(text, self.render_html(sections, now), settings=cfg)
        if ok:
            self._mark_sent(now)
        else:
            logger.warning(f"Отчёт по расписанию не отправлен: {message}")
        return ok, message

    def send_now(self, metrics: Optional[List[str]] = None) -> Tuple[bool, str]:
        cfg = self.settings()
        sections = self.collect(metrics or list(cfg.get("metrics") or METRIC_CODES))
        if not cfg.get("smtp_host"):
            return False, "не задан SMTP-сервер (админка → Отчёты на почту)"
        ok, message = self.send(self.render_text(sections),
                                self.render_html(sections), settings=cfg)
        if ok:
            self._mark_sent()
        return ok, message

    def _mark_sent(self, when: Optional[datetime] = None) -> None:
        try:
            from src.api.services.config_store import config_store
            cfg = self.settings()
            cfg["last_sent"] = (when or datetime.now()).isoformat(timespec="seconds")
            config_store.set("system", "reports", cfg)
        except Exception as e:
            logger.debug(f"не удалось запомнить время отправки: {e}")


# ── вспомогательные ──────────────────────────────────────────────────────────

def _gb(value: float) -> str:
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.1f} ГБ"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.0f} МБ"
    return f"{value / 1024:.0f} КБ"


def _ru_date(value: str) -> str:
    return value


def _parse_dt(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:19 if "%S" in fmt else 16], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _sql(query: str) -> List[Tuple[Any, ...]]:
    """Простой запрос к БД приложения (отчёт не должен тянуть ORM и кэши)."""
    try:
        from sqlalchemy import text

        from src.database.session import get_session_local
        factory = get_session_local()
        if factory is None:
            return []
        session = factory()
        try:
            return list(session.execute(text(query)).fetchall())
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Отчёт: запрос не прошёл ({query[:40]}…): {e}")
        return []


def _tcp(host: str, port: int, timeout: float = 3.0) -> Tuple[bool, str]:
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{(time.time() - started) * 1000:.0f} мс"
    except Exception as e:
        return False, type(e).__name__


def _redis_ping(host: str, port: int, timeout: float = 3.0) -> Tuple[bool, str]:
    """PING без библиотеки redis: достаточно сокета и протокола RESP."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(b"*1\r\n$4\r\nPING\r\n")
            reply = s.recv(64)
            return (b"PONG" in reply), ""
    except Exception as e:
        return False, type(e).__name__


def _http_ok(url: str, timeout: float = 5.0) -> Tuple[bool, str]:
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return (200 <= r.status < 400), f"{(time.time() - started) * 1000:.0f} мс"
    except Exception as e:
        return False, type(e).__name__


def _http_json(url: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _neo4j_count() -> Optional[int]:
    """Число узлов графа через HTTP-транзакции Neo4j (пароль из окружения)."""
    password = os.environ.get("NEO4J_PASSWORD", "")
    user = os.environ.get("NEO4J_USER", "neo4j")
    host = os.environ.get("NEO4J_HOST", "kag-neo4j")
    port = os.environ.get("NEO4J_HTTP_PORT", "7474")
    if not password:
        return None
    import base64
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    body = json.dumps({"statements": [{"statement": "MATCH (n) RETURN count(n) AS c"}]}).encode()
    req = urllib.request.Request(f"http://{host}:{port}/db/neo4j/tx/commit", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Basic {auth}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        return int(data["results"][0]["data"][0]["row"][0])
    except Exception:
        return None


def _du(path: str, timeout: float = 60.0) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _run(cmd: List[str], timeout: float = 20.0) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.stdout if res.returncode == 0 else ""
    except Exception:
        return ""


report_service = ReportService()


async def scheduler_loop(interval_s: int = 60) -> None:
    """Планировщик отчётов: раз в минуту проверяем, не пора ли отправить.

    Живёт в процессе api. Первую проверку делает через полминуты после старта, чтобы не мешать
    подъёму сервиса; все ошибки глушатся и пишутся в лог — упавший отчёт не должен ронять API.
    """
    await asyncio.sleep(30)
    while True:
        try:
            result = await asyncio.to_thread(report_service.run_due)
            if result is not None:
                ok, message = result
                logger.info(f"Отчёт по расписанию: {'отправлен' if ok else 'не отправлен'} — {message}")
        except Exception as e:
            logger.warning(f"Планировщик отчётов: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_s)
