"""
Модуль оценки качества для многоагентной системы KAG

Отвечает за:
- Оценку качества генерации (faithfulness, relevance, hallucination rate)
- Ручную оценку с аннотированием
- A/B-тестирование версий промптов/моделей
- Экспорт результатов оценки
- Сбор и анализ метрик качества
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
from enum import Enum
import json
import csv
from pathlib import Path
from loguru import logger
from pydantic import BaseModel, Field


class QualityMetric(Enum):
    """Метрики качества"""
    FAITHFULNESS = "faithfulness"  # Соответствие источникам
    RELEVANCE = "relevance"  # Релевантность запросу
    HALLUCINATION_RATE = "hallucination_rate"  # Уровень галлюцинаций
    COHERENCE = "coherence"  # Связность текста
    FLUENCY = "fluency"  # Гладкость текста
    COMPLETENESS = "completeness"  # Полнота ответа


class AnnotationScore(Enum):
    """Оценки аннотатора"""
    VERY_BAD = 1
    BAD = 2
    ACCEPTABLE = 3
    GOOD = 4
    EXCELLENT = 5


class Annotation(BaseModel):
    """Модель ручной оценки"""
    annotation_id: str = Field(..., description="ID оценки")
    response_id: str = Field(..., description="ID ответа")
    annotator_id: str = Field(..., description="ID аннотатора")
    scores: Dict[QualityMetric, AnnotationScore] = Field(
        ..., description="Оценки по метрикам"
    )
    comments: Optional[str] = Field(default=None, description="Комментарии")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Время создания")


class QualityReport(BaseModel):
    """Отчет о качестве"""
    report_id: str = Field(..., description="ID отчета")
    generated_at: datetime = Field(default_factory=datetime.utcnow, description="Время генерации")
    total_responses: int = Field(default=0, description="Всего ответов оценено")
    automatic_metrics: Dict[QualityMetric, float] = Field(
        default_factory=dict, description="Автоматические метрики"
    )
    manual_metrics: Dict[QualityMetric, float] = Field(
        default_factory=dict, description="Ручные метрики"
    )
    annotations_count: int = Field(default=0, description="Количество аннотаций")
    trends: Dict[str, List[float]] = Field(
        default_factory=dict, description="Тренды метрик"
    )


class QualityEvaluator:
    """
    Оценщик качества генерации.

    Поддерживает автоматические и ручные метрики качества.
    """

    def __init__(self, storage_path: Optional[Path] = None):
        """
        Инициализация оценщика.

        Args:
            storage_path: Путь для хранения аннотаций
        """
        self._annotations: List[Annotation] = []
        self._storage_path = storage_path or Path("/app/data/annotations")
        self._storage_path.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"QualityEvaluator инициализирован, хранение: {self._storage_path}")

    def evaluate_automatic(
        self,
        query: str,
        response: str,
        context: Optional[str] = None
    ) -> Dict[QualityMetric, float]:
        """
        Автоматическая оценка качества ответа.

        Args:
            query: Пользовательский запрос
            response: Сгенерированный ответ
            context: Контекст/источники (опционально)

        Returns:
            Словарь с метриками качества (0.0 - 1.0)
        """
        logger.debug(f"Автоматическая оценка: query={len(query)} символов, response={len(response)} символов")
        
        metrics = {
            QualityMetric.FAITHFULNESS: self._calculate_faithfulness(response, context),
            QualityMetric.RELEVANCE: self._calculate_relevance(query, response),
            QualityMetric.HALLUCINATION_RATE: self._calculate_hallucination_rate(response, context),
            QualityMetric.COHERENCE: self._calculate_coherence(response),
            QualityMetric.FLUENCY: self._calculate_fluency(response),
            QualityMetric.COMPLETENESS: self._calculate_completeness(query, response)
        }

        logger.info(f"Автоматическая оценка завершена: {len(metrics)} метрик")
        return metrics

    def _calculate_faithfulness(self, response: str, context: Optional[str] = None) -> float:
        """
        Рассчитать соответствие источникам.

        Оценивает, насколько ответ основан на предоставленном контексте.
        """
        if not context:
            return 0.5  # Не можем оценить без контекста

        # TODO: Реализовать через NLI модель или LLM-судью
        # Пока заглушка - в будущем здесь будет:
        # 1. Извлечение фактов из ответа
        # 2. Проверка каждого факта против контекста
        # 3. Расчет доли подтвержденных фактов
        
        return 0.7  # Заглушка

    def _calculate_relevance(self, query: str, response: str) -> float:
        """
        Рассчитать релевантность запросу.

        Оценивает, насколько ответ соответствует запросу.
        """
        # TODO: Реализовать через косинусное сходство эмбеддингов
        # Пока простая эвристический метод
        
        query_words = set(query.lower().split())
        response_words = set(response.lower().split())
        
        if not query_words:
            return 0.0
        
        overlap = len(query_words & response_words) / len(query_words)
        return min(1.0, overlap * 2)  # Нормализуем

    def _calculate_hallucination_rate(self, response: str, context: Optional[str] = None) -> float:
        """
        Рассчитать уровень галлюцинаций.

        Оценивает долю информации в ответе, не подтвержденной контекстом.
        """
        if not context:
            return 0.5  # Не можем оценить без контекста

        # TODO: Реализовать через проверку фактов
        # Пока заглушка
        
        return 0.2  # Заглушка (меньше - лучше)

    def _calculate_coherence(self, response: str) -> float:
        """
        Рассчитать связность текста.

        Оценивает логическую связность ответа.
        """
        # TODO: Реализовать через анализ переходов и структуры
        # Простая эвристика: проверяем наличие предложений
        
        sentences = [s.strip() for s in response.split('.') if s.strip()]
        
        if len(sentences) == 0:
            return 0.0
        elif len(sentences) == 1:
            return 0.5
        
        # Больше предложений - потенциально лучше связность
        return min(1.0, len(sentences) / 10)

    def _calculate_fluency(self, response: str) -> float:
        """
        Рассчитать гладкость текста.

        Оценивает grammatic correctness и читаемость.
        """
        # TODO: Реализовать через языковую модель
        # Простая проверка на пустоту и длину
        
        if not response.strip():
            return 0.0
        
        # Базовая проверка: есть ли текст
        word_count = len(response.split())
        return min(1.0, word_count / 20)  # Нормализуем

    def _calculate_completeness(self, query: str, response: str) -> float:
        """
        Рассчитать полноту ответа.

        Оценивает, насколько полно ответ покрывает запрос.
        """
        # TODO: Реализовать через проверку ключевых аспектов
        # Пока заглушка
        
        query_len = len(query)
        response_len = len(response)
        
        if query_len == 0:
            return 0.0
        
        # Эвристика: ответ должен быть длиннее запроса
        ratio = response_len / query_len
        return min(1.0, ratio / 5)  # Ответ в 5 раз длиннее запроса = 1.0

    def add_annotation(
        self,
        response_id: str,
        annotator_id: str,
        scores: Dict[QualityMetric, AnnotationScore],
        comments: Optional[str] = None
    ) -> Annotation:
        """
        Добавить ручную оценку.

        Args:
            response_id: ID оцениваемого ответа
            annotator_id: ID аннотатора
            scores: Оценки по метрикам
            comments: Комментарии аннотатора

        Returns:
            Созданная аннотация
        """
        import hashlib
        import time
        
        annotation_id = hashlib.md5(f"{response_id}-{annotator_id}-{time.time()}".encode()).hexdigest()[:12]
        
        annotation = Annotation(
            annotation_id=annotation_id,
            response_id=response_id,
            annotator_id=annotator_id,
            scores=scores,
            comments=comments
        )
        
        self._annotations.append(annotation)
        
        # Сохраняем на диск
        self._save_annotation(annotation)
        
        logger.info(f"Аннотация добавлена: {annotation_id}, аннотатор: {annotator_id}")
        return annotation

    def _save_annotation(self, annotation: Annotation):
        """Сохранить аннотацию в файл"""
        annotation_file = self._storage_path / f"{annotation.annotation_id}.json"
        
        data = annotation.model_dump()
        # Преобразуем enum в строки
        data['scores'] = {k.value: v.value for k, v in annotation.scores.items()}
        data['created_at'] = annotation.created_at.isoformat()
        
        with open(annotation_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_annotations(
        self,
        response_id: Optional[str] = None,
        annotator_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Annotation]:
        """Получить аннотации с фильтрацией"""
        annotations = self._annotations
        
        if response_id:
            annotations = [a for a in annotations if a.response_id == response_id]
        
        if annotator_id:
            annotations = [a for a in annotations if a.annotator_id == annotator_id]
        
        return annotations[-limit:]

    def generate_report(self) -> QualityReport:
        """
        Сгенерировать отчет о качестве.

        Returns:
            Отчет с агрегированными метриками
        """
        import hashlib
        
        if not self._annotations:
            logger.warning("Нет аннотаций для отчета")
            return QualityReport(
                report_id=hashlib.md5(datetime.utcnow().isoformat().encode()).hexdigest()[:16]
            )

        # Агрегация ручных оценок
        manual_metrics = {}
        for metric in QualityMetric:
            scores = []
            for annotation in self._annotations:
                if metric in annotation.scores:
                    # Нормализуем 1-5 в 0-1
                    scores.append(annotation.scores[metric].value / 5.0)
            
            if scores:
                manual_metrics[metric] = sum(scores) / len(scores)

        report_id = hashlib.md5(datetime.utcnow().isoformat().encode()).hexdigest()[:16]
        
        report = QualityReport(
            report_id=report_id,
            total_responses=len(self._annotations),
            manual_metrics=manual_metrics,
            annotations_count=len(self._annotations)
        )
        
        logger.info(f"Отчет сгенерирован: {report_id}, аннотаций: {len(self._annotations)}")
        return report

    def export_to_csv(self, output_path: Path) -> Path:
        """
        Экспортировать аннотации в CSV.

        Args:
            output_path: Путь для сохранения

        Returns:
            Путь к созданному файлу
        """
        if not self._annotations:
            logger.warning("Нет данных для экспорта")
            return output_path

        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            
            # Заголовок
            writer.writerow([
                'annotation_id',
                'response_id',
                'annotator_id',
                *[f'{m.value}' for m in QualityMetric],
                'comments',
                'created_at'
            ])
            
            # Данные
            for annotation in self._annotations:
                writer.writerow([
                    annotation.annotation_id,
                    annotation.response_id,
                    annotation.annotator_id,
                    *[annotation.scores.get(m, '').value if m in annotation.scores else '' for m in QualityMetric],
                    annotation.comments or '',
                    annotation.created_at.isoformat()
                ])

        logger.info(f"Аннотации экспортированы в CSV: {output_path}")
        return output_path

    def load_annotations_from_json(self, json_path: Path):
        """Загрузить аннотации из JSON файла"""
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if isinstance(data, list):
            for item in data:
                # Преобразуем строки обратно в enum
                scores = {
                    QualityMetric(k): AnnotationScore(v)
                    for k, v in item.get('scores', {}).items()
                }
                
                annotation = Annotation(
                    annotation_id=item['annotation_id'],
                    response_id=item['response_id'],
                    annotator_id=item['annotator_id'],
                    scores=scores,
                    comments=item.get('comments'),
                    created_at=datetime.fromisoformat(item['created_at'])
                )
                
                self._annotations.append(annotation)
        
        logger.info(f"Загружено {len(self._annotations)} аннотаций из {json_path}")


# Глобальный экземпляр оценщика
evaluator = QualityEvaluator()
