"""Спорная разметка: происхождение значений, ручная правка и правило «ручное правит только админ».

Главное, что здесь защищается, — не форма JSON, а два правила:
  1. значение, поставленное ЧЕЛОВЕКОМ, помечено в метаданных и НЕ перезаписывается моделью/прибором;
  2. править ручное значение может только администратор (сотрудник — только свои документы).

Второе правило легко потерять при рефакторинге: оно живёт в проверке внутри роута, а не в схеме
данных, поэтому проверяется и тестом, и живой пробой (401/403) на стенде.
"""
import asyncio
import json
import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.routes import labels as labels_mod
from src.indexing import label_provenance as prov


# ── Провенанс: форма и правила хранения ───────────────────────────────────────

def test_низкая_уверенность_помечается_как_спорная():
    p = prov.mark_model({}, "rubrics", ["banking"], 0.53, [["banking", 0.53], ["economics", 0.57]])
    assert prov.get(p, "rubrics")["flagged"] is True
    assert prov.disputed_fields(p) == ["rubrics"]
    assert prov.alternatives(p, "rubrics", 2) == [["banking", 0.53], ["economics", 0.57]]


def test_высокая_уверенность_не_спорная_но_происхождение_хранится():
    p = prov.mark_model({}, "facets.normative_force", ["informational"], 0.95)
    assert prov.get(p, "facets.normative_force")["flagged"] is False
    assert prov.disputed_fields(p) == []
    assert prov.confidence(p, "facets.normative_force") == 0.95


def test_ручная_правка_перекрывает_машинную_и_хранит_её_для_контекста():
    p = prov.mark_model({}, "rubrics", ["law"], 0.44)
    p = prov.mark_manual(p, ["rubrics"], by="ivanov", note="это про экономику")
    meta = prov.get(p, "rubrics")
    assert meta["source"] == "manual" and meta["by"] == "ivanov"
    assert meta["note"] == "это про экономику"
    assert meta["model_confidence"] == 0.44, "машинный ответ сохраняем: иначе не объяснить решение"


def test_модель_не_перезаписывает_ручное_значение():
    p = prov.mark_manual({}, ["rubrics"], by="ivanov")
    p = prov.mark_model(p, "rubrics", ["infosec"], 0.99)
    assert prov.is_manual(p, "rubrics"), "ручное значение не должно затираться новым прогоном модели"
    assert prov.get(p, "rubrics")["source"] == "manual"


def test_спорные_не_включают_ручные_поля():
    p = prov.mark_model({}, "rubrics", ["law"], 0.40)
    p = prov.mark_model(p, "facets.normative_force", ["informational"], 0.50)
    p = prov.mark_manual(p, ["rubrics"], by="ivanov")
    assert prov.disputed_fields(p) == ["facets.normative_force"]


def test_снятие_пометки_возвращает_поле_модели():
    p = prov.mark_manual({}, ["rubrics"], by="ivanov")
    p = prov.clear_manual(p, ["rubrics"], by="admin")
    assert prov.is_manual(p, "rubrics") is False
    assert prov.get(p, "rubrics")["manual_cleared_by"] == "admin"


def test_битый_json_не_роняет_разбор():
    assert prov.parse("{не json") == {}
    assert prov.parse(None) == {}
    assert prov.parse(json.dumps({"rubrics": {"source": "manual"}}))["rubrics"]["source"] == "manual"


# ── Валидация значений по словарю ─────────────────────────────────────────────

def test_неизвестная_тема_отвергается_с_подсказкой():
    ok, message, _ = labels_mod._validate_value("rubrics", "кибербезопасность")
    assert ok is False and "словаре" in message


def test_известная_тема_приводится_к_словарю():
    ok, _, value = labels_mod._validate_value("rubrics", ["LAW", "law", "infosec"])
    assert ok is True and value == ["infosec", "law"]


def test_значение_вне_перечня_фасета_отвергается():
    ok, message, _ = labels_mod._validate_value("facets.protection_subject", "котики")
    assert ok is False and "перечня" in message


def test_неизвестный_фасет_отвергается():
    ok, message, _ = labels_mod._validate_value("facets.облако", "data")
    assert ok is False and "фасет" in message


def test_поле_вне_поддержки_отвергается():
    ok, message, _ = labels_mod._validate_value("issuer", "ФСТЭК")
    assert ok is False and "не поддерживается" in message


# ── Правило доступа: только свои документы, ручное — только админ ─────────────

class _Repo:
    """Заглушка хранилища: в тесте важны не SQL, а проверки прав и записанное происхождение."""

    def __init__(self, docs):
        self.docs = docs
        self.written: list = []

    def get_dict(self, doc_id):
        return self.docs.get(doc_id)

    def get_all(self):
        return self.docs

    def upsert(self, doc_id, data):
        self.docs.setdefault(doc_id, {}).update(data)
        self.written.append((doc_id, dict(data)))


def _user(uid="u1", name="ivanov", admin=False):
    return types.SimpleNamespace(id=uid, username=name, is_admin=admin, email=f"{name}@x")


@pytest.fixture
def repo(monkeypatch):
    docs = {
        "doc-own": {"id": "doc-own", "filename": "a.pdf", "uploaded_by": "u1",
                    "rubrics": json.dumps(["law"]),
                    "label_provenance": prov.dump(prov.mark_model({}, "rubrics", ["law"], 0.44))},
        "doc-other": {"id": "doc-other", "filename": "b.pdf", "uploaded_by": "u2",
                      "rubrics": json.dumps(["infosec"]),
                      "label_provenance": prov.dump(prov.mark_model({}, "rubrics", ["infosec"], 0.50))},
        "doc-manual": {"id": "doc-manual", "filename": "c.pdf", "uploaded_by": "u1",
                       "rubrics": json.dumps(["economics"]),
                       "label_provenance": prov.dump(prov.mark_manual(
                           prov.mark_model({}, "rubrics", ["law"], 0.5), ["rubrics"], by="petrov"))},
    }
    r = _Repo(docs)
    # Роут берёт репозиторий внутри функции — подменяем на уровне модуля.
    import src.api.services.document_repository as drepo
    monkeypatch.setattr(drepo, "get_doc_repo", lambda: r)
    # Внешние слои (Qdrant payload, журнал) в тесте не нужны и не должны стучаться наружу.
    import sys
    stub = types.ModuleType("src.indexing.embeddings_service")

    class _Svc:
        async def update_document_payload(self, *a, **k):
            return None

    stub.service_for_document = lambda _doc_id: _Svc()
    monkeypatch.setitem(sys.modules, "src.indexing.embeddings_service", stub)
    journal = types.ModuleType("src.security.provenance")
    journal.append_action = lambda **k: None
    journal.actions = lambda limit=30: []
    monkeypatch.setitem(sys.modules, "src.security.provenance", journal)
    return r


def test_решение_человека_пишет_ручную_пометку(repo):
    res = asyncio.run(labels_mod.decide(
        labels_mod.Decision(document_id="doc-own", field="rubrics", value=["economics"],
                            note="это про экономику"), current_user=_user()))
    assert res["status"] == "ok"
    stored = json.loads(repo.docs["doc-own"]["rubrics"])
    assert stored == ["economics"]
    meta = prov.parse(repo.docs["doc-own"]["label_provenance"])["rubrics"]
    assert meta["source"] == "manual" and meta["by"] == "ivanov"
    assert meta["note"] == "это про экономику"
    assert meta["model_confidence"] == 0.44


def test_чужой_документ_сотруднику_не_доступен(repo):
    with pytest.raises(HTTPException) as e:
        asyncio.run(labels_mod.decide(
            labels_mod.Decision(document_id="doc-other", field="rubrics", value=["law"]),
            current_user=_user()))
    assert e.value.status_code == 403
    assert repo.docs["doc-other"]["rubrics"] == json.dumps(["infosec"]), "чужой документ не тронут"


def test_ручное_значение_сотруднику_не_переписать(repo):
    with pytest.raises(HTTPException) as e:
        asyncio.run(labels_mod.decide(
            labels_mod.Decision(document_id="doc-manual", field="rubrics", value=["infosec"]),
            current_user=_user()))
    assert e.value.status_code == 403
    assert "администратор" in e.value.detail


def test_администратор_правит_ручное_значение(repo):
    res = asyncio.run(labels_mod.decide(
        labels_mod.Decision(document_id="doc-manual", field="rubrics", value=["infosec"],
                            note="уточнил"),
        current_user=_user(uid="a1", name="admin", admin=True)))
    assert res["status"] == "ok"
    assert json.loads(repo.docs["doc-manual"]["rubrics"]) == ["infosec"]
    assert prov.parse(repo.docs["doc-manual"]["label_provenance"])["rubrics"]["by"] == "admin"


def test_значение_вне_словаря_не_пишется(repo):
    with pytest.raises(HTTPException) as e:
        asyncio.run(labels_mod.decide(
            labels_mod.Decision(document_id="doc-own", field="rubrics", value=["чепуха"]),
            current_user=_user()))
    assert e.value.status_code == 400
    assert json.loads(repo.docs["doc-own"]["rubrics"]) == ["law"]


def test_список_показывает_только_свои_документы(repo):
    mine = asyncio.run(labels_mod.review_list(limit=100, only="", current_user=_user()))
    ids = {i["document_id"] for i in mine["items"]}
    assert ids == {"doc-own", "doc-manual"}
    assert mine["can_admin"] is False

    all_docs = asyncio.run(labels_mod.review_list(limit=100, only="",
                                                 current_user=_user(admin=True)))
    assert {i["document_id"] for i in all_docs["items"]} == {"doc-own", "doc-other", "doc-manual"}


def test_фильтр_только_ручные(repo):
    only_manual = asyncio.run(labels_mod.review_list(limit=100, only="manual",
                                                    current_user=_user(admin=True)))
    assert [i["kind"] for i in only_manual["items"]] == ["manual"]
    assert only_manual["counts"]["manual"] == 1


def test_без_провенанса_спор_не_выдумывается(repo):
    """Документ со значением, но без записи о происхождении на страницу не попадает.

    Это осознанное правило: «нет провенанса» означает «о значении ничего не известно», а не «модель
    сомневалась». Показывать такие документы как спорные — значит звать человека разбирать неизвестно
    что. Провенанс заполняет прибор разметки, и после него документы с низкой уверенностью появляются.
    """
    repo.docs["doc-noprov"] = {"id": "doc-noprov", "filename": "d.pdf", "uploaded_by": "u1",
                               "rubrics": json.dumps(["it"]), "label_provenance": "{}"}
    items = asyncio.run(labels_mod.review_list(limit=100, only="", current_user=_user()))["items"]
    assert "doc-noprov" not in {i["document_id"] for i in items}


def test_пустая_тема_показывается_как_неразмеченная(repo):
    """Документ без темы — работа для человека, и это отдельный ВИД, а не «спорное значение модели»."""
    repo.docs["doc-empty"] = {"id": "doc-empty", "filename": "e.pdf", "uploaded_by": "u1",
                              "rubrics": "[]", "label_provenance": "{}"}
    items = asyncio.run(labels_mod.review_list(limit=100, only="", current_user=_user()))["items"]
    row = [i for i in items if i["document_id"] == "doc-empty"]
    assert row and row[0]["kind"] == "disputed" and row[0]["value"] in ([], "", None)


def test_ручная_правка_не_редактируется_сотрудником_но_видна(repo):
    items = asyncio.run(labels_mod.review_list(limit=100, only="manual",
                                               current_user=_user()))["items"]
    assert items and items[0]["editable"] is False
    assert "администратор" in (items[0]["blocked_reason"] or "")


# ── Роут: правило проверяется на уровне HTTP-зависимости ──────────────────────

def _client(monkeypatch, user, admin_ok):
    app = FastAPI()
    app.include_router(labels_mod.router, prefix="/api/v1/labels")
    app.dependency_overrides[labels_mod.get_current_user] = lambda: user

    def _admin():
        if not admin_ok:
            raise HTTPException(status_code=403, detail="Admin access required")
        return user

    app.dependency_overrides[labels_mod.get_current_admin] = _admin
    return TestClient(app)


def test_разблокировка_закрыта_для_не_администратора(repo, monkeypatch):
    client = _client(monkeypatch, _user(), admin_ok=False)
    r = client.post("/api/v1/labels/review/unlock",
                    json={"document_id": "doc-manual", "field": "rubrics"})
    assert r.status_code == 403


def test_разблокировка_работает_у_администратора(repo, monkeypatch):
    client = _client(monkeypatch, _user(admin=True), admin_ok=True)
    r = client.post("/api/v1/labels/review/unlock",
                    json={"document_id": "doc-manual", "field": "rubrics"})
    assert r.status_code == 200 and r.json()["manual"] is False
    assert prov.is_manual(prov.parse(repo.docs["doc-manual"]["label_provenance"]), "rubrics") is False


def test_роут_разбора_не_под_админским_префиксом():
    """Сотрудник должен иметь доступ: middleware закрывает только /api/v1/admin.

    Проверка структурная: она ловит перенос разбора под админский префикс (тогда сотрудник получит
    403 и страница станет бесполезной) — этого в коде не видно по вызовам, только по префиксу.
    """
    from src.api import main as main_mod
    src = (main_mod.__file__ and open(main_mod.__file__, encoding="utf-8").read()) or ""
    assert 'app.include_router(labels.router, prefix="/api/v1/labels"' in src
    assert "/api/v1/admin/labels" not in src


# ── Импорт происхождения не перезаписывает ручные значения ────────────────────

def test_импорт_провенанса_пропускает_ручные_поля(repo, monkeypatch):
    from src.api.routes import admin_models as am

    payload = am.LabelProvenanceBatch(items=[
        am.LabelProvenanceItem(document_id="doc-own", field="rubrics", value=["law"],
                               confidence=0.95),
        am.LabelProvenanceItem(document_id="doc-manual", field="rubrics", value=["law"],
                               confidence=0.99),
    ])
    res = asyncio.run(am.import_label_provenance(payload))
    assert res["status"] == "ok" and res["skipped_manual"] == 1
    assert prov.get(prov.parse(repo.docs["doc-own"]["label_provenance"]), "rubrics")["flagged"] is False
    assert prov.is_manual(prov.parse(repo.docs["doc-manual"]["label_provenance"]), "rubrics")
