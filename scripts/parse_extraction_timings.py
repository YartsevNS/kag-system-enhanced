"""Разбор фазовых таймингов извлечения: где реально уходит время прогона.

Читает шаг `typedb_timings` из лога процесса (config_store) и печатает таблицу:
фаза, сумма по документу, доля, среднее на фрагмент. Плюс разброс по фрагментам
(p50/p95/max), разбивка пустые/непустые и число ретраев.

Запуск в контейнере kag-api:
    python /tmp/parse_timings.py <document_id>
"""
import json
import sys

from src.api.services import pipeline

DOC = sys.argv[1] if len(sys.argv) > 1 else "fb4d9a84-e723-41fa-970d-2ed75683674d"

# берём сырые записи лога, чтобы найти шаг таймингов именно последнего прогона
from src.indexing.process_logger import get_process_log  # noqa: E402

entries = get_process_log(DOC) or []
# последний прогон — от последнего "start"/"find_file"
starts = [i for i, e in enumerate(entries) if str(e.get("step")) in ("start", "find_file")]
run = entries[starts[-1]:] if starts else entries
timing_steps = [e for e in run if e.get("step") == "typedb_timings"]
write_steps = [e for e in run if e.get("step") == "typedb_write"]

print("документ:", DOC)
print("записей в последнем прогоне:", len(run))
if write_steps:
    print("typedb_write:", json.dumps(write_steps[-1].get("details") or {}, ensure_ascii=False))
if not timing_steps:
    print("шага typedb_timings нет — прогон шёл на образе без таймеров")
    raise SystemExit(0)

t = timing_steps[-1].get("details") or {}
total_ms = float(t.get("wall_p50_ms") or 0) * int(t.get("fragments") or 0) or 0
# делим по фазам: сумма — из агрегата
phases = [
    ("вызов модели (LLM)", "llm_ms"),
    ("ожидание замка", "lock_wait_ms"),
    ("чтения из TypeDB", "reads_ms"),
    ("вставки в TypeDB", "insert_ms"),
    ("проверка целостности", "integrity_ms"),
    ("commit", "commit_ms"),
    ("очередь на семафоре", "queue_ms"),
    ("валидация/сборка (_collect)", "collect_ms"),
]
sum_all = sum(float(t.get(k) or 0) for _, k in phases)
frags = int(t.get("fragments") or 0) or 1
print(f"\nфрагментов: {frags}; фазы (сумма {sum_all/1000:.1f} с)")
for title, key in phases:
    v = float(t.get(key) or 0)
    share = (v / sum_all * 100) if sum_all else 0
    print(f"  {title:28} {v/1000:8.1f} с  {share:5.1f}%  avg {v/frags:7.1f} мс/фрагмент")

print("\nразброс по фрагментам:")
print(f"  p50 {t.get('wall_p50_ms')} мс · p95 {t.get('wall_p95_ms')} мс · max {t.get('wall_max_ms')} мс")
print(f"  пустые: n={t.get('empty_n')} avg={t.get('empty_avg_ms')} мс")
print(f"  с фактами: n={t.get('filled_n')} avg={t.get('filled_avg_ms')} мс")
print(f"  ретраев записи: {t.get('retries')}")
print(f"  символы: промпт {t.get('prompt_chars')}, ответ {t.get('payload_chars')}")
