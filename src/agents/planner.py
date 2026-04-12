"""
Планировщик задач для многоагентной системы KAG

Отвечает за:
- Анализ пользовательских запросов
- Декомпозицию задач на подзадачи
- Определение порядка выполнения
- Выбор необходимых инструментов и агентов
- Обработку зависимостей между задачами
"""

from typing import Dict, Any, Optional, List
from enum import Enum
from datetime import datetime
import hashlib
from loguru import logger
from pydantic import BaseModel, Field


class TaskPriority(Enum):
    """Приоритеты задач"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskType(Enum):
    """Типы задач"""
    SEARCH = "search"
    ANALYZE = "analyze"
    GENERATE = "generate"
    EVALUATE = "evaluate"
    INDEX = "index"
    TRANSCRIBE = "transcribe"
    SUMMARIZE = "summarize"


class TaskStatus(Enum):
    """Статусы задач"""
    PENDING = "pending"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    """Модель задачи"""
    task_id: str = Field(..., description="Уникальный идентификатор задачи")
    task_type: TaskType = Field(..., description="Тип задачи")
    priority: TaskPriority = Field(default=TaskPriority.MEDIUM, description="Приоритет")
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="Статус выполнения")
    description: str = Field(..., description="Описание задачи")
    parent_task_id: Optional[str] = Field(default=None, description="ID родительской задачи")
    dependencies: List[str] = Field(default_factory=list, description="ID зависимых задач")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Метаданные задачи")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Время создания")
    started_at: Optional[datetime] = Field(default=None, description="Время начала выполнения")
    completed_at: Optional[datetime] = Field(default=None, description="Время завершения")
    error_message: Optional[str] = Field(default=None, description="Сообщение об ошибке")


class Plan(BaseModel):
    """Модель плана выполнения"""
    plan_id: str = Field(..., description="Уникальный идентификатор плана")
    original_query: str = Field(..., description="Исходный запрос пользователя")
    tasks: List[Task] = Field(default_factory=list, description="Список задач")
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="Статус плана")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Время создания")
    estimated_completion: Optional[datetime] = Field(
        default=None, description="Оценочное время завершения"
    )


class AgentPlanner:
    """
    Планировщик задач для многоагентной системы.

    Анализирует запросы, создает планы выполнения и управляет задачами.
    """

    def __init__(self):
        """Инициализация планировщика"""
        self._plans: Dict[str, Plan] = {}
        self._task_counter = 0
        logger.info("AgentPlanner инициализирован")

    def _generate_task_id(self) -> str:
        """Сгенерировать уникальный ID задачи"""
        self._task_counter += 1
        timestamp = datetime.utcnow().isoformat()
        raw = f"{timestamp}-{self._task_counter}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _generate_plan_id(self) -> str:
        """Сгенерировать уникальный ID плана"""
        timestamp = datetime.utcnow().isoformat()
        return hashlib.md5(timestamp.encode()).hexdigest()[:16]

    def analyze_query(self, query: str) -> Dict[str, Any]:
        """
        Анализировать пользовательский запрос и определить необходимые действия.

        Args:
            query: Пользовательский запрос

        Returns:
            Словарь с результатами анализа
        """
        logger.debug(f"Анализ запроса: {query[:100]}...")

        # Базовая эвристика для определения типа запроса
        query_lower = query.lower()
        task_types = []

        # Определение типов задач по ключевым словам
        if any(word in query_lower for word in ["найди", "поиск", "найти", "search"]):
            task_types.append(TaskType.SEARCH)

        if any(word in query_lower for word in ["проанализируй", "анализ", "analyze"]):
            task_types.append(TaskType.ANALYZE)

        if any(word in query_lower for word in ["создай", "сгенерируй", "напиши", "generate"]):
            task_types.append(TaskType.GENERATE)

        if any(word in query_lower for word in ["оцени", "проверь", "evaluate"]):
            task_types.append(TaskType.EVALUATE)

        if any(word in query_lower for word in ["загрузи", "индексируй", "index"]):
            task_types.append(TaskType.INDEX)

        if any(word in query_lower for word in ["транскрибируй", "аудио", "transcribe"]):
            task_types.append(TaskType.TRANSCRIBE)

        if any(word in query_lower for word in ["резюмируй", "кратко", "summarize"]):
            task_types.append(TaskType.SUMMARIZE)

        # Если не удалось определить - добавляем поиск и генерацию по умолчанию
        if not task_types:
            task_types = [TaskType.SEARCH, TaskType.GENERATE]

        result = {
            "query": query,
            "detected_types": [t.value for t in task_types],
            "complexity": self._estimate_complexity(task_types),
            "requires_multi_agent": len(task_types) > 1,
            "estimated_tasks": len(task_types)
        }

        logger.info(f"Запрос проанализирован: {len(task_types)} типов задач")
        return result

    def _estimate_complexity(self, task_types: List[TaskType]) -> str:
        """Оценить сложность запроса"""
        if len(task_types) <= 1:
            return "simple"
        elif len(task_types) <= 3:
            return "medium"
        else:
            return "complex"

    def create_plan(self, query: str, metadata: Optional[Dict[str, Any]] = None) -> Plan:
        """
        Создать план выполнения для запроса.

        Args:
            query: Пользовательский запрос
            metadata: Дополнительные метаданные

        Returns:
            План выполнения
        """
        logger.info(f"Создание плана для запроса: {query[:100]}...")

        # Анализ запроса
        analysis = self.analyze_query(query)

        # Создание плана
        plan = Plan(
            plan_id=self._generate_plan_id(),
            original_query=query,
            status=TaskStatus.PLANNED
        )

        # Генерация задач на основе анализа
        task_types = [TaskType(t) for t in analysis["detected_types"]]
        previous_task_id = None

        for task_type in task_types:
            task = self._create_task(
                task_type=task_type,
                query=query,
                dependencies=[previous_task_id] if previous_task_id else [],
                metadata=metadata or {}
            )
            plan.tasks.append(task)
            previous_task_id = task.task_id

        # Оценка времени выполнения
        plan.estimated_completion = self._estimate_completion_time(plan.tasks)

        # Сохранение плана
        self._plans[plan.plan_id] = plan

        logger.info(
            f"План создан: {plan.plan_id}, "
            f"задач: {len(plan.tasks)}, "
            f"сложность: {analysis['complexity']}"
        )

        return plan

    def _create_task(
        self,
        task_type: TaskType,
        query: str,
        dependencies: List[str],
        metadata: Dict[str, Any]
    ) -> Task:
        """Создать отдельную задачу"""
        task = Task(
            task_id=self._generate_task_id(),
            task_type=task_type,
            priority=self._determine_priority(task_type, metadata),
            status=TaskStatus.PLANNED,
            description=f"{task_type.value}: {query[:200]}",
            dependencies=dependencies,
            metadata={
                **metadata,
                "original_query": query,
                "task_type": task_type.value
            }
        )

        logger.debug(f"Задача создана: {task.task_id}, тип: {task_type.value}")
        return task

    def _determine_priority(self, task_type: TaskType, metadata: Dict[str, Any]) -> TaskPriority:
        """Определить приоритет задачи"""
        # Если явно указан приоритет в метаданных
        if "priority" in metadata:
            try:
                return TaskPriority(metadata["priority"])
            except ValueError:
                pass

        # По умолчанию - средний приоритет
        return TaskPriority.MEDIUM

    def _estimate_completion_time(self, tasks: List[Task]) -> datetime:
        """Оценить время завершения плана"""
        # Базовая оценка: 30 секунд на задачу
        base_time = len(tasks) * 30
        from datetime import timedelta
        return datetime.utcnow() + timedelta(seconds=base_time)

    def get_plan(self, plan_id: str) -> Optional[Plan]:
        """Получить план по ID"""
        return self._plans.get(plan_id)

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        error_message: Optional[str] = None
    ) -> bool:
        """
        Обновить статус задачи.

        Args:
            task_id: ID задачи
            status: Новый статус
            error_message: Сообщение об ошибке (если есть)

        Returns:
            True если задача обновлена
        """
        for plan in self._plans.values():
            for task in plan.tasks:
                if task.task_id == task_id:
                    task.status = status
                    task.error_message = error_message

                    if status == TaskStatus.IN_PROGRESS:
                        task.started_at = datetime.utcnow()
                    elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                        task.completed_at = datetime.utcnow()

                    logger.info(f"Статус задачи обновлен: {task_id} -> {status.value}")
                    return True

        logger.warning(f"Задача не найдена: {task_id}")
        return False

    def get_execution_order(self, plan_id: str) -> List[str]:
        """
        Получить порядок выполнения задач с учетом зависимостей.

        Args:
            plan_id: ID плана

        Returns:
            Список ID задач в порядке выполнения
        """
        plan = self._plans.get(plan_id)
        if not plan:
            logger.error(f"План не найден: {plan_id}")
            return []

        # Топологическая сортировка
        visited = set()
        order = []

        def visit(task_id: str):
            if task_id in visited:
                return

            visited.add(task_id)

            # Находим задачу
            task = next((t for t in plan.tasks if t.task_id == task_id), None)
            if not task:
                return

            # Сначала посещаем зависимости
            for dep_id in task.dependencies:
                visit(dep_id)

            order.append(task_id)

        # Начинаем с задач без зависимостей
        for task in plan.tasks:
            visit(task.task_id)

        return order

    def get_plan_status(self, plan_id: str) -> Dict[str, Any]:
        """Получить статус выполнения плана"""
        plan = self._plans.get(plan_id)
        if not plan:
            return {"error": "Plan not found"}

        completed = sum(1 for t in plan.tasks if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in plan.tasks if t.status == TaskStatus.FAILED)
        in_progress = sum(1 for t in plan.tasks if t.status == TaskStatus.IN_PROGRESS)

        return {
            "plan_id": plan_id,
            "status": plan.status.value,
            "total_tasks": len(plan.tasks),
            "completed": completed,
            "failed": failed,
            "in_progress": in_progress,
            "pending": len(plan.tasks) - completed - failed - in_progress,
            "progress_percent": round((completed / len(plan.tasks)) * 100, 2) if plan.tasks else 0
        }

    def cancel_plan(self, plan_id: str) -> bool:
        """Отменить выполнение плана"""
        plan = self._plans.get(plan_id)
        if not plan:
            return False

        plan.status = TaskStatus.CANCELLED
        for task in plan.tasks:
            if task.status in (TaskStatus.PENDING, TaskStatus.PLANNED):
                task.status = TaskStatus.CANCELLED

        logger.info(f"План отменен: {plan_id}")
        return True

    def list_plans(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Получить список всех планов"""
        plans_list = []
        for plan in sorted(
            self._plans.values(),
            key=lambda p: p.created_at,
            reverse=True
        )[:limit]:
            plans_list.append({
                "plan_id": plan.plan_id,
                "query": plan.original_query[:100],
                "status": plan.status.value,
                "tasks_count": len(plan.tasks),
                "created_at": plan.created_at.isoformat()
            })
        return plans_list


# Глобальный экземпляр планировщика
planner = AgentPlanner()
