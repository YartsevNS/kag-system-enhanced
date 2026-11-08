"""Починить уже созданные записи журнала: в define_text вместо старой сборки — живой текст схемы.

Причина: до правки экспорта журнал сохранял текст, собранный из структуры API, без
аннотаций (@key/@card/@abstract). Такие записи помечены «скачивать не стоит», но это
сбивает с толку: схема в базе целая, неполон только сохранённый текст.
"""
import json
import os
import sys

sys.path.insert(0, "/app")

from src.api.services.ontology_service import ontology_service  # noqa: E402
from src.api.services.ontology_versions import snapshot_of  # noqa: E402
from src.database.ontology_models import OntologySchemaVersion  # noqa: E402
from src.database.session import get_session_local  # noqa: E402

live = ontology_service.live_schema_text()
print("живой текст схемы:", live.count("\n"), "строк, @key:", live.count("@key"))

Session = get_session_local()
db = Session()
try:
    rows = (db.query(OntologySchemaVersion)
            .order_by(OntologySchemaVersion.created_at.desc()).limit(50).all())
    fixed = 0
    for r in rows:
        text = r.define_text or ""
        if "@key" in text or "@card" in text or "@abstract" in text:
            continue
        r.define_text = live
        try:
            r.snapshot_json = json.dumps(snapshot_of(ontology_service.get_schema()),
                                         ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        fixed += 1
    db.commit()
    print("исправлено записей журнала:", fixed)
finally:
    db.close()
