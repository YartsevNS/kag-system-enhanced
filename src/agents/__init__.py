"""
Модуль многоагентного взаимодействия KAG

Обеспечивает:
- Планирование задач (planner)
- Выполнение задач (executor)
- Оценку качества (evaluator)
"""

from src.agents.planner import AgentPlanner, planner, Task, Plan, TaskType, TaskStatus, TaskPriority
from src.agents.executor import AgentExecutor, executor, ExecutionResult
from src.agents.evaluator import QualityEvaluator, evaluator, Annotation, QualityReport, QualityMetric

__all__ = [
    "AgentPlanner",
    "planner",
    "AgentExecutor", 
    "executor",
    "QualityEvaluator",
    "evaluator",
    "Task",
    "Plan",
    "TaskType",
    "TaskStatus",
    "TaskPriority",
    "ExecutionResult",
    "Annotation",
    "QualityReport",
    "QualityMetric"
]
