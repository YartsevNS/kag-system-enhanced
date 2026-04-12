"""
Модуль оценки качества для системы KAG

Обеспечивает:
- Автоматические метрики качества
- LLM-судья для оценки
- A/B тестирование
- Трекинг трендов качества
"""

from src.evaluation.quality import (
    LLMJudgeConfig,
    LLMJudgeEvaluator,
    QualityTracker,
    llm_judge,
    quality_tracker
)
from src.evaluation.ab_testing import (
    VariantType,
    TestVariant,
    ABTest,
    ABTestResult,
    ABTestManager,
    ab_test_manager
)

__all__ = [
    "LLMJudgeConfig",
    "LLMJudgeEvaluator",
    "QualityTracker",
    "llm_judge",
    "quality_tracker",
    "VariantType",
    "TestVariant",
    "ABTest",
    "ABTestResult",
    "ABTestManager",
    "ab_test_manager"
]
