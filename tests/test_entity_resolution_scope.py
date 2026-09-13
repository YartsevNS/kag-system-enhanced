"""Дедупликация сущностей: из конвейера убрана, кандидаты — в maintenance-задаче.

Замеры, из-за которых решение принято: эмбеддинг всех имён графа + косинусы O(n²)
стоили ~44 с НА КАЖДЫЙ документ (1580 обрабатываемых сущностей / 8 в батче × 225 мс),
давали ~0.6% кандидатов от графа и применяли слияния при сходстве >=0.95 без ревью.
Качество кандидатов низкое: «российских банков» → «кредитных организаций-респондентов»
(sim=0.857) — это разные сущности.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = (ROOT / "src/api/services/document_service.py").read_text(encoding="utf-8")
TASKS = (ROOT / "src/indexing/tasks.py").read_text(encoding="utf-8")
KG = (ROOT / "src/indexing/knowledge_graph.py").read_text(encoding="utf-8")
CELERY = (ROOT / "src/indexing/celery_app.py").read_text(encoding="utf-8")


def test_pipeline_does_not_run_entity_resolution():
    """Дорогой проход по всему графу не должен идти в обработке каждого документа."""
    assert "to_thread(kg_service.resolve_duplicate_entities" not in PIPELINE
    assert "resolve_duplicate_entities(" not in PIPELINE.replace(
        "# Кандидаты теперь формирует ночная задача", ""
    ), "в конвейере не должно быть вызовов resolve_duplicate_entities"


def test_pipeline_still_applies_reviewed_aliases():
    """Словарь алиасов (только reviewed) остаётся в конвейере — он детерминированный."""
    assert "apply_alias_pairs" in PIPELINE
    assert "link_document_versions" in PIPELINE


def test_maintenance_task_generates_candidates_only():
    """Ночная задача: кандидаты без автослияний."""
    assert "def resolve_entity_candidates" in TASKS
    assert "auto_merge=False" in TASKS
    # Задача должна идти в maintenance-очередь, а не к основному воркеру документов
    i = TASKS.index("def resolve_entity_candidates")
    decorator = TASKS[max(0, i - 400):i]
    assert 'queue="maintenance"' in decorator


def test_candidates_task_is_scheduled_daily():
    assert "resolve-entity-candidates" in CELERY
    assert "86400.0" in CELERY


def test_resolution_flag_exists_and_skips_merges():
    """auto_merge=False должен именно пропускать слияния, а не молча сливать."""
    assert "auto_merge: bool = True" in KG
    assert "if merge_plan and auto_merge:" in KG
    assert "if merge_plan and not auto_merge:" in KG
