"""Переключение схемы извлечения графа: current (прежняя) | v2 (новая).

Настройка — `system/graph_quality.extraction_scheme`. Пишем и СРАЗУ читаем обратно, чтобы видеть
фактический результат, а не «команда выполнена». Чтение в другом процессе каждый раз новое:
кэш настроек в контейнере двухуровневый (статика 30 с), поэтому тем же процессом проверять
бессмысленно.

Запускать внутри контейнера api (и помнить: граф строит ВОРКЕР — настройку читают оба):
    docker exec kag-api python /tmp/set_extraction_scheme.py v2
    docker exec kag-api python /tmp/set_extraction_scheme.py current
    docker exec kag-api python /tmp/set_extraction_scheme.py          # только показать
"""
import sys

from src.api.services.config_store import config_store

ALLOWED = {"current", "v2"}


def main() -> int:
    cfg = dict(config_store.get("system", "graph_quality") or {})
    want = (sys.argv[1].strip().lower() if len(sys.argv) > 1 else "")
    if not want:
        print(f"сейчас: extraction_scheme = {cfg.get('extraction_scheme', 'current')!r}")
        print(f"допустимо: {sorted(ALLOWED)}")
        return 0
    if want not in ALLOWED:
        raise SystemExit(f"недопустимое значение {want!r}; допустимо: {sorted(ALLOWED)}")
    cfg["extraction_scheme"] = want
    ok = config_store.set("system", "graph_quality", cfg)
    back = (config_store.get("system", "graph_quality") or {}).get("extraction_scheme")
    print(f"запись принята: {ok} | прочитано обратно: {back!r}")
    print("OK" if ok and back == want else "ПРОВАЛ: настройка не сохранилась")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
