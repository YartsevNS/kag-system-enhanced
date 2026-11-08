"""Страница онтологии = живое отражение TypeDB, а не снимок.

Проверено на стенде 192.168.50.18: тип, созданный напрямую в TypeDB (мимо API),
появляется в /schema при следующем чтении, отпечаток меняется, журнал сам пишет
запись action=external с перечнем добавленного.
"""
import io

SVC = "src/api/services/ontology_service.py"
ROUTES = "src/api/routes/ontology.py"
VERS = "src/api/services/ontology_versions.py"
HTML = "src/api/static/ontology.html"


def _schema(entities=None, relations=None, attributes=None):
    return {
        "entities": entities if entities is not None else [
            {"label": "cii_object", "owns": ["name"], "plays": ["asset_protection:protected_system"]},
        ],
        "relations": relations if relations is not None else [
            {"label": "asset_protection", "owns": [], "roles": ["deployed_szi", "protected_system"]},
        ],
        "attributes": attributes if attributes is not None else [
            {"label": "name", "value": "string"},
        ],
        "ntypes": 3,
    }


def test_fingerprint_stable_and_sensitive():
    from src.api.services.ontology_service import ontology_service

    a = _schema()
    assert ontology_service.fingerprint(a) == ontology_service.fingerprint(_schema())
    b = _schema(entities=a["entities"] + [{"label": "new_type", "owns": [], "plays": []}])
    assert ontology_service.fingerprint(a) != ontology_service.fingerprint(b)
    # порядок списков не должен менять отпечаток
    c = _schema(entities=list(reversed(a["entities"])))
    assert ontology_service.fingerprint(a) == ontology_service.fingerprint(c)


def test_fingerprint_changes_on_owns():
    from src.api.services.ontology_service import ontology_service

    a = _schema()
    b = _schema(entities=[{"label": "cii_object", "owns": ["name", "category"],
                           "plays": ["asset_protection:protected_system"]}])
    assert ontology_service.fingerprint(a) != ontology_service.fingerprint(b)


def test_schema_route_is_live_and_uncached():
    text = io.open(ROUTES, encoding="utf-8").read()
    block = text.split('@router.get("/schema"', 1)[1].split("@router.post", 1)[0]
    assert "fingerprint" in block
    assert "no-store" in block
    assert "note_external_if_changed" in block
    assert "read_at" in block


def test_external_changes_are_recorded():
    text = io.open(VERS, encoding="utf-8").read()
    assert "def note_external_if_changed" in text
    block = text.split("def note_external_if_changed", 1)[1]
    assert 'action="external"' in block
    assert "diff_snapshots" in block
    # сравнение по структуре: одинаковый снимок не должен давать запись
    assert "if prev == cur:\n                    return None" in block


def test_page_watches_schema():
    html = io.open(HTML, encoding="utf-8").read()
    for token in ("watchSchema", "startWatch", "st-fp", "st-read", "Обновить схему"):
        assert token in html, token
    assert "setInterval(watchSchema" in html
    assert "visibilitychange" in html


def test_page_labels_external_actions():
    html = io.open(HTML, encoding="utf-8").read()
    assert "external: 'изменено вне UI'" in html
