"""
Тесты для модулей многоагентной системы KAG
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, patch, AsyncMock

from src.agents.planner import AgentPlanner, Task, Plan, TaskType, TaskStatus, TaskPriority
from src.agents.executor import AgentExecutor, ExecutionResult
from src.agents.evaluator import QualityEvaluator, QualityMetric, AnnotationScore


# ===========================================
# Тесты планировщика
# ===========================================

class TestAgentPlanner:
    """Тесты планировщика задач"""

    @pytest.fixture
    def planner(self):
        """Создать планировщик"""
        return AgentPlanner()

    def test_analyze_query_search(self, planner):
        """Тест анализа запроса на поиск"""
        result = planner.analyze_query("найди информацию о Python")
        
        assert "detected_types" in result
        assert "search" in result["detected_types"]
        assert result["complexity"] in ["simple", "medium", "complex"]

    def test_analyze_query_generate(self, planner):
        """Тест анализа запроса на генерацию"""
        result = planner.analyze_query("сгенерируй отчет")
        
        assert "generate" in result["detected_types"]

    def test_analyze_query_multi(self, planner):
        """Тест анализа сложного запроса"""
        result = planner.analyze_query("найди и проанализируй данные")
        
        assert len(result["detected_types"]) >= 2
        assert result["requires_multi_agent"] is True

    def test_create_plan_simple(self, planner):
        """Тест создания простого плана"""
        plan = planner.create_plan("найди информацию")
        
        assert isinstance(plan, Plan)
        assert plan.plan_id is not None
        assert len(plan.tasks) > 0
        assert plan.status == TaskStatus.PLANNED

    def test_create_plan_with_metadata(self, planner):
        """Тест создания плана с метаданными"""
        metadata = {"user_id": "test-user", "priority": "high"}
        plan = planner.create_plan("найди информацию", metadata=metadata)
        
        assert plan.plan_id is not None
        assert len(plan.tasks) > 0

    def test_get_plan(self, planner):
        """Тест получения плана"""
        plan = planner.create_plan("тестовый запрос")
        retrieved = planner.get_plan(plan.plan_id)
        
        assert retrieved is not None
        assert retrieved.plan_id == plan.plan_id

    def test_get_plan_not_found(self, planner):
        """Тест получения несуществующего плана"""
        retrieved = planner.get_plan("nonexistent-id")
        assert retrieved is None

    def test_update_task_status(self, planner):
        """Тест обновления статуса задачи"""
        plan = planner.create_plan("тестовый запрос")
        task_id = plan.tasks[0].task_id
        
        result = planner.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        
        assert result is True
        
        # Проверяем что статус обновился
        task = next(t for t in plan.tasks if t.task_id == task_id)
        assert task.status == TaskStatus.IN_PROGRESS

    def test_update_task_status_not_found(self, planner):
        """Тест обновления статуса несуществующей задачи"""
        result = planner.update_task_status("nonexistent", TaskStatus.COMPLETED)
        assert result is False

    def test_get_execution_order(self, planner):
        """Тест получения порядка выполнения"""
        plan = planner.create_plan("найди и проанализируй")
        order = planner.get_execution_order(plan.plan_id)
        
        assert isinstance(order, list)
        assert len(order) > 0
        # Все задачи должны быть в порядке
        assert set(order) == {t.task_id for t in plan.tasks}

    def test_get_execution_order_not_found(self, planner):
        """Тест получения порядка для несуществующего плана"""
        order = planner.get_execution_order("nonexistent")
        assert order == []

    def test_get_plan_status(self, planner):
        """Тест получения статуса плана"""
        plan = planner.create_plan("тестовый запрос")
        status = planner.get_plan_status(plan.plan_id)
        
        assert "plan_id" in status
        assert "status" in status
        assert "total_tasks" in status
        assert "progress_percent" in status
        assert status["total_tasks"] == len(plan.tasks)

    def test_cancel_plan(self, planner):
        """Тест отмены плана"""
        plan = planner.create_plan("тестовый запрос")
        result = planner.cancel_plan(plan.plan_id)
        
        assert result is True
        assert plan.status == TaskStatus.CANCELLED

    def test_cancel_plan_not_found(self, planner):
        """Тест отмены несуществующего плана"""
        result = planner.cancel_plan("nonexistent")
        assert result is False

    def test_list_plans(self, planner):
        """Тест списка планов"""
        # Создаем несколько планов
        planner.create_plan("запрос 1")
        planner.create_plan("запрос 2")
        planner.create_plan("запрос 3")
        
        plans = planner.list_plans()
        
        assert len(plans) == 3
        assert all("plan_id" in p for p in plans)
        assert all("query" in p for p in plans)

    def test_list_plans_with_limit(self, planner):
        """Тест списка планов с лимитом"""
        for i in range(10):
            planner.create_plan(f"запрос {i}")
        
        plans = planner.list_plans(limit=5)
        assert len(plans) == 5


# ===========================================
# Тесты исполнителя
# ===========================================

class TestAgentExecutor:
    """Тесты исполнителя задач"""

    @pytest.fixture
    def executor(self):
        """Создать исполнитель"""
        return AgentExecutor()

    @pytest.fixture
    def sample_task(self):
        """Создать тестовую задачу"""
        return Task(
            task_id="test-task-1",
            task_type=TaskType.SEARCH,
            priority=TaskPriority.MEDIUM,
            status=TaskStatus.PLANNED,
            description="Тестовая задача поиска",
            metadata={"original_query": "тест"}
        )

    @pytest.mark.asyncio
    async def test_execute_task_search(self, executor, sample_task):
        """Тест выполнения задачи поиска"""
        result = await executor.execute_task(sample_task)
        
        assert isinstance(result, ExecutionResult)
        assert result.task_id == sample_task.task_id
        assert result.status == TaskStatus.COMPLETED
        assert result.result is not None

    @pytest.mark.asyncio
    async def test_execute_task_generate(self, executor):
        """Тест выполнения задачи генерации"""
        task = Task(
            task_id="test-task-2",
            task_type=TaskType.GENERATE,
            description="Тестовая задача генерации",
            metadata={"original_query": "тест"}
        )
        
        result = await executor.execute_task(task)
        
        assert result.status == TaskStatus.COMPLETED
        assert "message" in result.result

    @pytest.mark.asyncio
    async def test_execute_task_evaluate(self, executor):
        """Тест выполнения задачи оценки"""
        task = Task(
            task_id="test-task-3",
            task_type=TaskType.EVALUATE,
            description="Тестовая задача оценки",
            metadata={"original_query": "тест"}
        )
        
        result = await executor.execute_task(task)
        
        assert result.status == TaskStatus.COMPLETED
        assert "metrics" in result.result

    @pytest.mark.asyncio
    async def test_execute_plan(self, executor):
        """Тест выполнения плана"""
        from src.agents.planner import planner
        
        plan = planner.create_plan("найди информацию")
        result = await executor.execute_plan(plan.plan_id)
        
        assert "plan_id" in result
        assert "status" in result
        assert "results" in result

    def test_get_execution_stats(self, executor):
        """Тест получения статистики"""
        stats = executor.get_execution_stats()
        
        assert "total_executions" in stats
        assert "completed" in stats
        assert "failed" in stats
        assert "success_rate" in stats
        assert "handlers_registered" in stats

    def test_register_handler(self, executor):
        """Тест регистрации обработчика"""
        def custom_handler(task):
            return {"custom": True}
        
        executor.register_handler(TaskType.SEARCH, custom_handler)
        assert TaskType.SEARCH in executor._handlers

    def test_clear_history(self, executor):
        """Тест очистки истории"""
        # Добавляем запись в историю
        executor._execution_history.append(
            ExecutionResult(
                task_id="test",
                status=TaskStatus.COMPLETED
            )
        )
        
        executor.clear_history()
        assert len(executor._execution_history) == 0


# ===========================================
# Тесты оценщика качества
# ===========================================

class TestQualityEvaluator:
    """Тесты оценщика качества"""

    @pytest.fixture
    def evaluator(self):
        """Создать оценщик"""
        return QualityEvaluator()

    def test_evaluate_automatic(self, evaluator):
        """Тест автоматической оценки"""
        query = "Что такое Python?"
        response = "Python - это язык программирования"
        
        metrics = evaluator.evaluate_automatic(query, response)
        
        assert isinstance(metrics, dict)
        assert QualityMetric.FAITHFULNESS in metrics
        assert QualityMetric.RELEVANCE in metrics
        assert QualityMetric.HALLUCINATION_RATE in metrics

    def test_evaluate_with_context(self, evaluator):
        """Тест оценки с контекстом"""
        query = "Что такое Python?"
        response = "Python - язык программирования"
        context = "Python - высокоуровневый язык"
        
        metrics = evaluator.evaluate_automatic(query, response, context)
        
        assert all(isinstance(v, float) for v in metrics.values())

    def test_add_annotation(self, evaluator):
        """Тест добавления аннотации"""
        annotation = evaluator.add_annotation(
            response_id="test-response",
            annotator_id="test-annotator",
            scores={
                QualityMetric.FAITHFULNESS: AnnotationScore.GOOD,
                QualityMetric.RELEVANCE: AnnotationScore.EXCELLENT
            },
            comments="Хороший ответ"
        )
        
        assert annotation.annotation_id is not None
        assert annotation.response_id == "test-response"
        assert annotation.annotator_id == "test-annotator"

    def test_get_annotations(self, evaluator):
        """Тест получения аннотаций"""
        evaluator.add_annotation(
            response_id="resp-1",
            annotator_id="ann-1",
            scores={QualityMetric.FAITHFULNESS: AnnotationScore.GOOD}
        )
        
        annotations = evaluator.get_annotations()
        assert len(annotations) >= 1

    def test_generate_report(self, evaluator):
        """Тест генерации отчета"""
        # Добавляем аннотации
        for i in range(3):
            evaluator.add_annotation(
                response_id=f"resp-{i}",
                annotator_id="ann-1",
                scores={QualityMetric.FAITHFULNESS: AnnotationScore.GOOD}
            )
        
        report = evaluator.generate_report()
        
        assert report.report_id is not None
        assert report.total_responses == 3
        assert report.annotations_count == 3

    def test_export_to_csv(self, evaluator, tmp_path):
        """Тест экспорта в CSV"""
        evaluator.add_annotation(
            response_id="resp-1",
            annotator_id="ann-1",
            scores={QualityMetric.FAITHFULNESS: AnnotationScore.GOOD}
        )
        
        output_file = tmp_path / "export.csv"
        result_path = evaluator.export_to_csv(output_file)
        
        assert result_path.exists()
        assert result_path.stat().st_size > 0
