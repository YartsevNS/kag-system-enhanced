"""Пересборка графа ОДНОГО документа ШТАТНОЙ задачей (то же, что делает кнопка «Перестроить граф»).

Зачем: замер схемы извлечения надо делать на КОНВЕЙЕРЕ, а не на щупе. Щуп со своей копией промпта
мерил не то, что работало (на этом расхождении прошлый заход потерял день: «щуп 81 %, конвейер 0 %»).
Здесь вызывается та же задача rebuild_graph_task из очереди maintenance: она чистит граф документа,
создаёт узлы фрагментов и зовёт entity_extractor.extract_and_store — то есть ТОТ ЖЕ код, что поедет
в работу, с выбором схемы по настройке system/graph_quality.extraction_scheme.

apply() выполняет задачу в этом процессе: без токена, без ожидания очереди; тот же код задачи.

    docker exec kag-system_worker-maintenance_1 python /tmp/rebuild_doc_graph.py <document_id>
"""

import sys
import time

DOC = sys.argv[1] if len(sys.argv) > 1 else "2873e687-d476-43e6-b94e-f4dd14eaead8"

from src.api.services.config_store import config_store  # noqa: E402
from src.indexing.tasks import rebuild_graph_task  # noqa: E402

_scheme = (config_store.get("system", "graph_quality") or {}).get("extraction_scheme", "current")
_model = ""
try:
    from src.api.services.provider_service import provider_service

    _model = (provider_service.get_function_llm_config("graph") or {}).get("model", "")
except Exception as e:  # noqa: BLE001
    _model = f"(модель не взята: {e})"

print("=" * 84)
print(f"ПЕРЕСБОРКА ГРАФА ДОКУМЕНТА {DOC}")
print(f"схема извлечения: {_scheme} | модель: {_model}")
print("=" * 84, flush=True)

t0 = time.perf_counter()
res = rebuild_graph_task.apply(args=[[DOC]]).get()
sec = time.perf_counter() - t0
print(f"\nзадача завершена за {sec:.1f} с ({sec / 60:.1f} мин)")
print("результат:", res)
