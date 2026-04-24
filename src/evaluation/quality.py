"""
Модуль оценки качества генерации для системы KAG

Автоматические метрики:
- Faithfulness (соответствие источникам)
- Relevance (релевантность запросу)
- Hallucination rate (уровень галлюцинаций)
- Coherence (связность)
- Fluency (гладкость)
- Completeness (полнота)

Интеграция с LLM-судьей для более точной оценки.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
from loguru import logger
from pydantic import BaseModel, Field

from src.agents.evaluator import QualityMetric, QualityEvaluator, evaluator


class LLMJudgeConfig(BaseModel):
    """Конфигурация LLM-судьи"""
    model_name: str = Field(default="gpt-4", description="Модель для оценки")
    temperature: float = Field(default=0.0, description="Температура (детерминированная)")
    max_tokens: int = Field(default=500, description="Максимум токенов")
    prompt_template: str = Field(
        default="""
Оцени качество ответа на запрос.

Запрос: {query}
Ответ: {response}
Контекст: {context}

Оцени по шкале от 1 до 5:
1. Faithfulness (соответствие контексту):
2. Relevance (релевантность запросу):
3. Hallucination rate (1=нет галлюцинаций, 5=много):

Ответь в формате JSON:
{{"faithfulness": N, "relevance": N, "hallucination_rate": N}}
""",
        description="Шаблон промпта для оценки"
    )


class LLMJudgeEvaluator:
    """
    Оценщик качества на основе LLM-судьи.

    Использует мощную LLM для оценки ответов другой LLM.
    """

    def __init__(self, config: Optional[LLMJudgeConfig] = None):
        """
        Инициализация LLM-судьи.

        Args:
            config: Конфигурация
        """
        self.config = config or LLMJudgeConfig()
        logger.info(f"LLMJudgeEvaluator инициализирован, модель: {self.config.model_name}")

    async def evaluate(
        self,
        query: str,
        response: str,
        context: Optional[str] = None
    ) -> Dict[QualityMetric, float]:
        """
        Оценить качество ответа через LLM-судью.

        Args:
            query: Пользовательский запрос
            response: Сгенерированный ответ
            context: Контекст/источники

        Returns:
            Словарь с метриками качества
        """
        logger.debug(f"LLM оценка качества: query={len(query)}, response={len(response)}")

        # TODO: Интеграция с реальной LLM
        # from openai import AsyncOpenAI
        # client = AsyncOpenAI(...)
        # prompt = self.config.prompt_template.format(
        #     query=query, response=response, context=context or "Нет контекста"
        # )
        # result = await client.chat.completions.create(
        #     model=self.config.model_name,
        #     messages=[{"role": "user", "content": prompt}],
        #     temperature=self.config.temperature,
        #     max_tokens=self.config.max_tokens
        # )
        # scores = json.loads(result.choices[0].message.content)

        # Заглушка для демонстрации
        scores = {
            QualityMetric.FAITHFULNESS: 0.75,
            QualityMetric.RELEVANCE: 0.80,
            QualityMetric.HALLUCINATION_RATE: 0.15,
            QualityMetric.COHERENCE: 0.85,
            QualityMetric.FLUENCY: 0.90,
            QualityMetric.COMPLETENESS: 0.70
        }

        logger.info(f"LLM оценка завершена: {len(scores)} метрик")
        return scores

    def create_evaluation_prompt(self, query: str, response: str, context: str) -> str:
        """Создать промпт для оценки"""
        return self.config.prompt_template.format(
            query=query,
            response=response,
            context=context or "Нет контекста"
        )


class QualityTracker:
    """
    Трекер качества для мониторинга трендов.

    Собирает метрики качества за период времени
    и предоставляет аналитику.
    """

    def __init__(self, storage_path: Optional[Path] = None):
        """
        Инициализация трекера.

        Args:
            storage_path: Путь для хранения данных
        """
        self._storage_path = storage_path or Path("/app/data/quality_tracking")
        try:
            self._storage_path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            logger.warning("Не могу создать /app/data, использую /tmp для quality_tracking")
            self._storage_path = Path("/tmp/kag_quality_tracking")
            self._storage_path.mkdir(parents=True, exist_ok=True)
        
        self._metrics_history: List[Dict[str, Any]] = []
        
        logger.info(f"QualityTracker инициализирован, хранение: {self._storage_path}")

    def record_metrics(
        self,
        metrics: Dict[QualityMetric, float],
        query: str,
        response: str,
        model: str,
        user_id: Optional[str] = None
    ):
        """
        Записать метрики качества.

        Args:
            metrics: Метрики качества
            query: Запрос
            response: Ответ
            model: Использованная модель
            user_id: ID пользователя
        """
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": {k.value: v for k, v in metrics.items()},
            "query_length": len(query),
            "response_length": len(response),
            "model": model,
            "user_id": user_id
        }

        self._metrics_history.append(record)

        # Сохраняем на диск периодически
        if len(self._metrics_history) % 100 == 0:
            self._save_to_disk()

        logger.debug(f"Метрики записаны: {len(self._metrics_history)} записей всего")

    def _save_to_disk(self):
        """Сохранить историю на диск"""
        output_file = self._storage_path / "metrics_history.json"
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(self._metrics_history, f, ensure_ascii=False, indent=2)
        
        logger.info(f"История метрик сохранена: {output_file}")

    def get_trends(
        self,
        metric: QualityMetric,
        window_size: int = 100
    ) -> List[float]:
        """
        Получить тренд метрики.

        Args:
            metric: Метрика для анализа
            window_size: Размер окна

        Returns:
            Список значений метрики
        """
        recent = self._metrics_history[-window_size:]
        return [
            record["metrics"].get(metric.value, 0.0)
            for record in recent
        ]

    def get_average_metrics(
        self,
        window_size: int = 100
    ) -> Dict[QualityMetric, float]:
        """
        Получить средние метрики за период.

        Args:
            window_size: Размер окна

        Returns:
            Средние значения метрик
        """
        recent = self._metrics_history[-window_size:]
        
        if not recent:
            return {}

        # Агрегация по метрикам
        aggregates = {}
        for record in recent:
            for metric_name, value in record["metrics"].items():
                if metric_name not in aggregates:
                    aggregates[metric_name] = []
                aggregates[metric_name].append(value)

        # Расчет средних
        return {
            QualityMetric(name): sum(values) / len(values)
            for name, values in aggregates.items()
        }

    def get_model_comparison(
        self,
        models: List[str],
        metric: QualityMetric
    ) -> Dict[str, float]:
        """
        Сравнить модели по метрике.

        Args:
            models: Список моделей для сравнения
            metric: Метрика для сравнения

        Returns:
            Словарь {модель: среднее_значение}
        """
        model_scores = {model: [] for model in models}

        for record in self._metrics_history:
            model = record.get("model")
            if model in model_scores:
                value = record["metrics"].get(metric.value, 0.0)
                model_scores[model].append(value)

        return {
            model: (sum(scores) / len(scores) if scores else 0.0)
            for model, scores in model_scores.items()
        }

    def export_report(self, output_path: Path) -> Path:
        """
        Экспортировать отчет о качестве.

        Args:
            output_path: Путь для сохранения

        Returns:
            Путь к файлу отчета
        """
        report = {
            "generated_at": datetime.utcnow().isoformat(),
            "total_records": len(self._metrics_history),
            "average_metrics": self.get_average_metrics(),
            "trends": {
                metric.value: self.get_trends(metric, window_size=50)
                for metric in QualityMetric
            }
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        logger.info(f"Отчет экспортирован: {output_path}")
        return output_path


# Глобальные экземпляры
llm_judge = LLMJudgeEvaluator()
quality_tracker = QualityTracker()
