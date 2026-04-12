"""
Исполнитель задач для многоагентной системы KAG

Отвечает за:
- Выполнение задач согласно плану
- Управление потоком выполнения
- Обработку ошибок и повторных попыток
- Координацию между агентами
- Сбор результатов выполнения
"""

from typing import Dict, Any, Optional, List, Callable, Awaitable
from datetime import datetime
import asyncio
from loguru import logger
from pydantic import BaseModel, Field

from src.agents.planner import (
    Task, TaskStatus, TaskType, Plan, TaskPriority,
    planner
)


class ExecutionResult(BaseModel):
    """Результат выполнения задачи"""
    task_id: str = Field(..., description="ID задачи")
    status: TaskStatus = Field(..., description="Статус выполнения")
    result: Optional[Dict[str, Any]] = Field(
        default=None, description="Результат выполнения"
    )
    error: Optional[str] = Field(default=None, description="Ошибка выполнения")
    duration_seconds: float = Field(default=0.0, description="Время выполнения")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Дополнительные метаданные")


class AgentExecutor:
    """
    Исполнитель задач многоагентной системы.

    Управляет выполнением задач, обрабатывает ошибки и собирает результаты.
    """

    def __init__(self):
        """Инициализация исполнителя"""
        self._handlers: Dict[TaskType, Callable] = {}
        self._execution_history: List[ExecutionResult] = []
        self._max_retries = 3
        self._retry_delay = 1.0  # секунды
        
        # Регистрация обработчиков
        self._register_default_handlers()
        
        logger.info("AgentExecutor инициализирован")

    def _register_default_handlers(self):
        """Регистрация обработчиков по умолчанию для каждого типа задач"""
        self._handlers = {
            TaskType.SEARCH: self._handle_search,
            TaskType.ANALYZE: self._handle_analyze,
            TaskType.GENERATE: self._handle_generate,
            TaskType.EVALUATE: self._handle_evaluate,
            TaskType.INDEX: self._handle_index,
            TaskType.TRANSCRIBE: self._handle_transcribe,
            TaskType.SUMMARIZE: self._handle_summarize,
        }
        logger.debug("Обработчики задач зарегистрированы")

    def register_handler(self, task_type: TaskType, handler: Callable):
        """
        Зарегистрировать пользовательский обработчик.

        Args:
            task_type: Тип задачи
            handler: Функция-обработчик
        """
        self._handlers[task_type] = handler
        logger.info(f"Обработчик зарегистрирован: {task_type.value}")

    async def execute_plan(self, plan_id: str) -> Dict[str, Any]:
        """
        Выполнить весь план целиком.

        Args:
            plan_id: ID плана для выполнения

        Returns:
            Словарь с результатами выполнения плана
        """
        plan = planner.get_plan(plan_id)
        if not plan:
            error_msg = f"План не найден: {plan_id}"
            logger.error(error_msg)
            return {"error": error_msg, "status": "failed"}

        logger.info(f"Начало выполнения плана: {plan_id}")
        plan.status = TaskStatus.IN_PROGRESS

        # Получаем порядок выполнения с учетом зависимостей
        execution_order = planner.get_execution_order(plan_id)
        
        results = {}
        failed_tasks = []

        # Выполняем задачи по порядку
        for task_id in execution_order:
            # Находим задачу в плане
            task = next((t for t in plan.tasks if t.task_id == task_id), None)
            if not task:
                logger.error(f"Задача не найдена: {task_id}")
                continue

            # Проверяем зависимости
            if not self._check_dependencies(task, results):
                error_msg = f"Зависимости не выполнены для задачи {task_id}"
                logger.warning(error_msg)
                planner.update_task_status(task_id, TaskStatus.FAILED, error_msg)
                failed_tasks.append(task_id)
                continue

            # Выполняем задачу
            result = await self.execute_task(task)
            results[task_id] = result

            if result.status == TaskStatus.FAILED:
                failed_tasks.append(task_id)
                logger.error(f"Задача не выполнена: {task_id}")
            else:
                logger.info(f"Задача выполнена: {task_id}")

        # Обновляем статус плана
        if failed_tasks:
            plan.status = TaskStatus.FAILED
            logger.warning(f"План выполнен с ошибками: {len(failed_tasks)} задач не выполнено")
        else:
            plan.status = TaskStatus.COMPLETED
            logger.info(f"План успешно выполнен: {plan_id}")

        return {
            "plan_id": plan_id,
            "status": plan.status.value,
            "results": results,
            "failed_tasks": failed_tasks,
            "total_tasks": len(execution_order),
            "completed_tasks": len(execution_order) - len(failed_tasks)
        }

    def _check_dependencies(
        self,
        task: Task,
        results: Dict[str, ExecutionResult]
    ) -> bool:
        """Проверить, выполнены ли все зависимости задачи"""
        for dep_id in task.dependencies:
            if dep_id not in results:
                return False
            if results[dep_id].status != TaskStatus.COMPLETED:
                return False
        return True

    async def execute_task(self, task: Task) -> ExecutionResult:
        """
        Выполнить отдельную задачу с обработкой ошибок и повторных попыток.

        Args:
            task: Задача для выполнения

        Returns:
            Результат выполнения
        """
        logger.info(f"Выполнение задачи: {task.task_id}, тип: {task.task_type.value}")
        
        # Обновляем статус на IN_PROGRESS
        planner.update_task_status(task.task_id, TaskStatus.IN_PROGRESS)
        
        start_time = datetime.utcnow()
        last_error = None

        # Попытки выполнения
        for attempt in range(self._max_retries):
            try:
                logger.debug(f"Попытка {attempt + 1}/{self._max_retries}: {task.task_id}")
                
                # Получаем обработчик для типа задачи
                handler = self._handlers.get(task.task_type)
                if not handler:
                    raise ValueError(f"Нет обработчика для типа: {task.task_type.value}")

                # Выполняем задачу
                if asyncio.iscoroutinefunction(handler):
                    result = await handler(task)
                else:
                    result = handler(task)

                # Успех
                duration = (datetime.utcnow() - start_time).total_seconds()
                execution_result = ExecutionResult(
                    task_id=task.task_id,
                    status=TaskStatus.COMPLETED,
                    result=result,
                    duration_seconds=duration
                )

                # Обновляем статус
                planner.update_task_status(task.task_id, TaskStatus.COMPLETED)
                self._execution_history.append(execution_result)

                logger.info(
                    f"Задача выполнена: {task.task_id}, "
                    f"время: {duration:.2f}с, попыток: {attempt + 1}"
                )
                
                return execution_result

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"Ошибка выполнения {task.task_id} "
                    f"(попытка {attempt + 1}/{self._max_retries}): {e}"
                )
                
                if attempt < self._max_retries - 1:
                    # Ждем перед следующей попыткой (экспоненциальная задержка)
                    delay = self._retry_delay * (2 ** attempt)
                    logger.debug(f"Пауза {delay}с перед повторной попыткой")
                    await asyncio.sleep(delay)

        # Все попытки исчерпаны
        duration = (datetime.utcnow() - start_time).total_seconds()
        execution_result = ExecutionResult(
            task_id=task.task_id,
            status=TaskStatus.FAILED,
            error=last_error,
            duration_seconds=duration
        )

        # Обновляем статус
        planner.update_task_status(task.task_id, TaskStatus.FAILED, last_error)
        self._execution_history.append(execution_result)

        logger.error(f"Задача провалена: {task.task_id}, ошибка: {last_error}")
        return execution_result

    # ===========================================
    # Обработчики задач (реализация)
    # ===========================================

    async def _handle_search(self, task: Task) -> Dict[str, Any]:
        """Обработчик задачи поиска"""
        logger.info(f"Поиск: {task.metadata.get('original_query', '')[:100]}")
        
        # TODO: Интеграция с Qdrant для векторного поиска
        # from src.indexing.vectorizer import Vectorizer
        # vectorizer = Vectorizer()
        # results = vectorizer.search(task.metadata.get('original_query', ''))
        
        # Заглушка для демонстрации
        return {
            "status": "search_completed",
            "results": [],
            "count": 0,
            "message": "Поиск выполнен (заглушка - требуется интеграция с Qdrant)"
        }

    async def _handle_analyze(self, task: Task) -> Dict[str, Any]:
        """Обработчик задачи анализа"""
        logger.info(f"Анализ: {task.metadata.get('original_query', '')[:100]}")
        
        # TODO: Реализовать анализ данных
        return {
            "status": "analyze_completed",
            "insights": [],
            "message": "Анализ выполнен (заглушка - требуется реализация)"
        }

    async def _handle_generate(self, task: Task) -> Dict[str, Any]:
        """Обработчик задачи генерации"""
        logger.info(f"Генерация: {task.metadata.get('original_query', '')[:100]}")
        
        # TODO: Интеграция с LLM (vLLM/Transformers)
        # from src.llm import LLMClient
        # llm = LLMClient()
        # response = await llm.generate(task.metadata.get('original_query', ''))
        
        return {
            "status": "generate_completed",
            "generated_text": "",
            "tokens_used": 0,
            "message": "Генерация выполнена (заглушка - требуется интеграция с LLM)"
        }

    async def _handle_evaluate(self, task: Task) -> Dict[str, Any]:
        """Обработчик задачи оценки"""
        logger.info(f"Оценка: {task.metadata.get('original_query', '')[:100]}")
        
        # TODO: Интеграция с модулем оценки качества
        # from src.evaluation.quality import QualityEvaluator
        # evaluator = QualityEvaluator()
        # metrics = evaluator.evaluate(task.metadata.get('generated_text', ''))
        
        return {
            "status": "evaluate_completed",
            "metrics": {
                "faithfulness": 0.0,
                "relevance": 0.0,
                "hallucination_rate": 0.0
            },
            "message": "Оценка выполнена (заглушка - требуется модуль оценки)"
        }

    async def _handle_index(self, task: Task) -> Dict[str, Any]:
        """Обработчик задачи индексации"""
        logger.info(f"Индексация: {task.metadata.get('original_query', '')[:100]}")
        
        # TODO: Интеграция с Celery задачами индексации
        # from src.indexing.tasks import process_document
        # task_result = process_document.delay(...)
        
        return {
            "status": "index_completed",
            "documents_indexed": 0,
            "message": "Индексация выполнена (заглушка - требуется интеграция)"
        }

    async def _handle_transcribe(self, task: Task) -> Dict[str, Any]:
        """Обработчик задачи транскрипции"""
        logger.info(f"Транскрипция: {task.metadata.get('original_query', '')[:100]}")
        
        # TODO: Интеграция с Whisper для транскрипции аудио
        # from src.indexing.tasks import transcribe_audio
        # result = transcribe_audio.delay(...)
        
        return {
            "status": "transcribe_completed",
            "transcript": "",
            "duration": 0.0,
            "message": "Транскрипция выполнена (заглушка - требуется Whisper)"
        }

    async def _handle_summarize(self, task: Task) -> Dict[str, Any]:
        """Обработчик задачи суммаризации"""
        logger.info(f"Суммаризация: {task.metadata.get('original_query', '')[:100]}")
        
        # TODO: Реализовать суммаризацию через LLM
        return {
            "status": "summarize_completed",
            "summary": "",
            "original_length": 0,
            "summary_length": 0,
            "message": "Суммаризация выполнена (заглушка - требуется LLM)"
        }

    # ===========================================
    # Утилиты и статистика
    # ===========================================

    def get_execution_stats(self) -> Dict[str, Any]:
        """Получить статистику выполнения"""
        total = len(self._execution_history)
        completed = sum(
            1 for r in self._execution_history if r.status == TaskStatus.COMPLETED
        )
        failed = sum(
            1 for r in self._execution_history if r.status == TaskStatus.FAILED
        )
        
        avg_duration = (
            sum(r.duration_seconds for r in self._execution_history) / total
            if total > 0
            else 0
        )

        return {
            "total_executions": total,
            "completed": completed,
            "failed": failed,
            "success_rate": round((completed / total) * 100, 2) if total > 0 else 0,
            "average_duration_seconds": round(avg_duration, 2),
            "handlers_registered": list(self._handlers.keys())
        }

    def get_execution_history(
        self,
        limit: int = 100,
        task_id: Optional[str] = None
    ) -> List[ExecutionResult]:
        """Получить историю выполнений"""
        history = self._execution_history
        
        if task_id:
            history = [r for r in history if r.task_id == task_id]
        
        return history[-limit:]

    def clear_history(self):
        """Очистить историю выполнений"""
        self._execution_history.clear()
        logger.info("История выполнений очищена")


# Глобальный экземпляр исполнителя
executor = AgentExecutor()
