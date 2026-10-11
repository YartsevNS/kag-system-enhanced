"""Строгий или мягкий режим проверки цитат.

Настройка `system/graph_quality.require_evidence`:
  false (мягкий) — связь пишется, даже если модель не подобрала цитату (evidence пустое);
  true (строгий) — пишутся только связи, чья цитата дословно найдена в тексте фрагмента.

    docker exec kag-api python /tmp/set_require_evidence.py true|false
    docker exec kag-api python /tmp/set_require_evidence.py        # показать текущее
"""
import sys

from src.api.services.config_store import config_store

want = (sys.argv[1].strip().lower() if len(sys.argv) > 1 else "")
cfg = dict(config_store.get("system", "graph_quality") or {})
if not want:
    print(f"сейчас: require_evidence = {bool(cfg.get('require_evidence', False))} | "
          f"схема = {cfg.get('extraction_scheme', 'current')!r}")
    raise SystemExit(0)
if want not in ("true", "false"):
    raise SystemExit("укажи true или false")
cfg["require_evidence"] = (want == "true")
ok = config_store.set("system", "graph_quality", cfg)
back = bool((config_store.get("system", "graph_quality") or {}).get("require_evidence"))
print(f"запись принята: {ok} | прочитано обратно: {back}")
print("OK" if ok and back == (want == "true") else "ПРОВАЛ: настройка не сохранилась")
