"""Доменная схема сущностей: хранение в настройках и применение в процессе.

Симптом, который закрывают тесты: настройку писали, но не читали. После рестарта
api пресет сбрасывался на «universal», а worker (в нём идёт извлечение сущностей)
выбранный в админке пресет не видел вообще — это другой процесс.

Плюс структурные проверки остальных пунктов обзора /kg (R1–R4, R6, R8, аудит-лог).
"""
from pathlib import Path

import pytest

from src.indexing import entity_extractor as ee
from src.indexing.entity_extractor import EntityExtractor, entity_extractor

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "src/api/routes/knowledge_graph.py").read_text(encoding="utf-8")


def _fake_config(value, boom=False):
    def loader():
        if boom:
            raise RuntimeError("БД недоступна")
        return value
    return loader


@pytest.fixture(autouse=True)
def _restore_state():
    """Схему и пресет после каждого теста возвращаем к исходным."""
    saved_preset = EntityExtractor._active_preset
    saved_schema = dict(entity_extractor._domain_config)
    saved_domain = EntityExtractor.DOMAIN_SCHEMA
    yield
    EntityExtractor._active_preset = saved_preset
    EntityExtractor.DOMAIN_SCHEMA = saved_domain
    entity_extractor._domain_config = saved_schema


def test_no_saved_config_keeps_default(monkeypatch):
    monkeypatch.setattr(ee, "_load_active_domain_config", _fake_config(None))
    assert entity_extractor.apply_stored_domain_schema() == "default"


def test_saved_preset_is_applied(monkeypatch):
    name = "infosec" if "infosec" in EntityExtractor.SCHEMA_PRESETS else list(EntityExtractor.SCHEMA_PRESETS)[0]
    monkeypatch.setattr(ee, "_load_active_domain_config", _fake_config({
        "mode": "preset", "preset": name, "schema": EntityExtractor.SCHEMA_PRESETS[name]["schema"],
    }))
    assert entity_extractor.apply_stored_domain_schema() == f"preset:{name}"
    assert EntityExtractor.get_active_preset() == name
    assert entity_extractor._domain_config == EntityExtractor.SCHEMA_PRESETS[name]["schema"]


def test_saved_manual_schema_is_applied(monkeypatch):
    manual = {"core": {"policy": {"label": "Политика", "color": "#fff"}}, "relations": {}}
    monkeypatch.setattr(ee, "_load_active_domain_config", _fake_config({
        "mode": "manual", "preset": None, "schema": manual,
    }))
    assert entity_extractor.apply_stored_domain_schema() == "manual"
    assert entity_extractor._domain_config == manual
    # Ручная схема не должна подсвечивать пресет в интерфейсе.
    assert EntityExtractor.get_active_preset() == "manual"


def test_unknown_preset_is_ignored(monkeypatch):
    monkeypatch.setattr(ee, "_load_active_domain_config", _fake_config({
        "mode": "preset", "preset": "не-существует", "schema": {},
    }))
    assert entity_extractor.apply_stored_domain_schema() == "default"


def test_broken_config_is_fail_open(monkeypatch):
    """Сбой чтения настроек не должен ломать извлечение — схема остаётся текущей."""
    monkeypatch.setattr(ee, "_load_active_domain_config", _fake_config(None, boom=True))
    before = dict(entity_extractor._domain_config)
    assert entity_extractor.apply_stored_domain_schema() == "default"
    assert entity_extractor._domain_config == before


def test_set_domain_schema_can_mark_manual():
    schema = {"core": {"x": {"label": "X", "color": "#000"}}}
    entity_extractor.set_domain_schema(schema, mark_manual=True)
    assert entity_extractor._domain_config == schema
    assert EntityExtractor.get_active_preset() == "manual"


def test_route_persists_schema_in_both_modes():
    start = ROUTES.index("async def update_domain_schema(")
    block = ROUTES[start:start + 2500]
    assert 'EntityExtractor.DOMAIN_ACTIVE_KEY' in block, "схему нужно сохранять в настройки"
    assert '"mode": "preset"' in block and '"mode": "manual"' in block, "режимы различаются явно"
    assert "mark_manual=True" in block, "ручная схема должна снимать отметку пресета"


def test_read_endpoint_reflects_settings():
    start = ROUTES.index("async def get_domain_schema(")
    block = ROUTES[start:start + 1200]
    assert "apply_stored_domain_schema" in block, (
        "GET /domain-schema обязан показывать состояние из настроек, а не из памяти процесса"
    )


def test_worker_applies_schema():
    src = (ROOT / "src/indexing/tasks.py").read_text(encoding="utf-8")
    start = src.index("def process_document(")
    assert "apply_stored_domain_schema" in src[start:start + 9000], (
        "worker — отдельный процесс: без применения схемы пресет из админки до него не доходит"
    )


def test_api_startup_applies_schema():
    src = (ROOT / "src/api/main.py").read_text(encoding="utf-8")
    assert "apply_stored_domain_schema" in src


# ── остальные пункты обзора ────────────────────────────────────────────────

def test_rebuild_graph_checks_running_before_cas():
    start = ROUTES.index("async def rebuild_graph(")
    block = ROUTES[start:start + 2200]
    assert block.index('status == "running"') < block.index("compare_and_set"), (
        "«уже идёт» должно проверяться до CAS, иначе два 409 говорят об одном и том же"
    )
    assert "[rebuild]" in block, "запуск перестроения должен попадать в лог"


def test_type_watchdog_status_single_pass():
    start = ROUTES.index("async def type_watchdog_status(")
    block = ROUTES[start:start + 1600]
    assert "for d in docs.values():" in block, "подсчёт должен идти одним проходом"
    assert "sum(1 for d in docs.values()" not in block, "второй проход по словарю не нужен"


def test_hybrid_search_hoists_query_lower():
    start = ROUTES.index("async def hybrid_search(")
    block = ROUTES[start:start + 4000]
    assert block.index("q_lower = q.lower()") < block.index("for point in (qdrant_results")


def test_document_graph_clamps_limits():
    start = ROUTES.index("async def document_graph(")
    block = ROUTES[start:start + 1200]
    assert "_clamp(limit_chunks" in block and "_clamp(limit_entities" in block, (
        "лимиты подграфа документа тоже должны клампиться, как в остальных роутах"
    )


def test_stats_has_no_unused_user_param():
    start = ROUTES.index("async def kg_stats(")
    signature = ROUTES[start:ROUTES.index(":", ROUTES.index("async def kg_stats(")) + 20]
    assert "current_user" not in signature.split(")")[0], "неиспользуемый параметр — мусор"


def test_embeddings_service_has_public_is_initialized():
    src = (ROOT / "src/indexing/embeddings_service.py").read_text(encoding="utf-8")
    assert "def is_initialized(self)" in src, "нужен публичный метод вместо обращения к приватному полю"
    start = ROUTES.index("async def hybrid_search(")
    assert "is_initialized()" in ROUTES[start:start + 4000]
    assert "_embedding_client" not in ROUTES, "роуты не должны знать про приватное поле сервиса"


def test_cypher_audit_log_has_context():
    start = ROUTES.index("async def execute_cypher(")
    block = ROUTES[start:start + 2000]
    assert "[cypher]" in block
    assert "rows=" in block and "limit=" in block and "user=" in block, (
        "в аудите должны быть пользователь, лимит и число строк результата"
    )
    assert "results=results" not in block, "содержимое результата в лог не пишем (там тексты и PII)"
