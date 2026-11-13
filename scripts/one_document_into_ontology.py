"""Один документ через поток: проверка, что наполнение онтологии работает.

Владелец 13.11.2026: не гнать корпус целиком, сначала добиться результата на одном
документе, потом на пяти — чтобы не обрабатывать то, что потом удалим.

Документ: fstek-2014-mery-zashchity-gis.pdf (321 фрагмент) — это «Меры защиты
информации в ГИС», откуда в онтологию обязаны попасть security_measure (АВЗ, ОПС, ЗНИ…).

Запуск внутри kag-api в фоне: процесс идёт минуты, SSH-сессия его не держит.
Результат и лог шагов — /tmp/onto_one.log.
"""
import asyncio
import json
import sys

DOC = sys.argv[1] if len(sys.argv) > 1 else "fb4d9a84-e723-41fa-970d-2ed75683674d"

from src.api.services.document_service import document_service  # noqa: E402

print("start", DOC, flush=True)
try:
    res = asyncio.run(document_service.process_document(DOC, force=True))
    print("RESULT", json.dumps(res, ensure_ascii=False, default=str)[:1500], flush=True)
except Exception as exc:  # noqa: BLE001
    print("FAILED", type(exc).__name__, str(exc)[:800], flush=True)
