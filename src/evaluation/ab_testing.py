"""
Модуль A/B тестирования для системы KAG

Отвечает за:
- Сравнение версий промптов
- Сравнение моделей LLM
- Статистический анализ результатов
- Визуализацию и отчеты
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
from enum import Enum
import json
import hashlib
from pathlib import Path
from loguru import logger
from pydantic import BaseModel, Field
import random


class VariantType(Enum):
    """Тип варианта"""
    PROMPT = "prompt"
    MODEL = "model"
    TEMPERATURE = "temperature"
    CHUNK_SIZE = "chunk_size"
    OTHER = "other"


class TestVariant(BaseModel):
    """Вариант для тестирования"""
    variant_id: str = Field(..., description="ID варианта")
    name: str = Field(..., description="Название варианта")
    variant_type: VariantType = Field(..., description="Тип варианта")
    config: Dict[str, Any] = Field(default_factory=dict, description="Конфигурация")
    traffic_percentage: float = Field(
        default=50.0,
        description="Процент трафика (0-100)",
        ge=0,
        le=100
    )


class ABTestResult(BaseModel):
    """Результат A/B теста"""
    test_id: str = Field(..., description="ID теста")
    variant_id: str = Field(..., description="ID варианта")
    samples: int = Field(default=0, description="Количество выборок")
    metrics: Dict[str, float] = Field(default_factory=dict, description="Метрики")
    start_time: datetime = Field(default_factory=datetime.utcnow, description="Время начала")
    end_time: Optional[datetime] = Field(default=None, description="Время окончания")


class ABTest:
    """
    A/B тест для сравнения вариантов.

    Поддерживает тестирование промптов, моделей и других параметров.
    """

    def __init__(
        self,
        test_id: str,
        name: str,
        test_type: VariantType,
        variants: List[TestVariant],
        storage_path: Optional[Path] = None
    ):
        """
        Инициализация A/B теста.

        Args:
            test_id: ID теста
            name: Название теста
            test_type: Тип теста
            variants: Варианты для сравнения
            storage_path: Путь для хранения
        """
        self.test_id = test_id
        self.name = name
        self.test_type = test_type
        self.variants = variants
        self._storage_path = storage_path or Path("/app/data/ab_tests")
        try:
            self._storage_path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            logger.warning("Не могу создать /app/data, использую /tmp для ab_tests")
            self._storage_path = Path("/tmp/kag_ab_tests")
            self._storage_path.mkdir(parents=True, exist_ok=True)
        
        self._results: Dict[str, ABTestResult] = {}
        self._start_time = datetime.utcnow()
        self._end_time: Optional[datetime] = None
        self._is_active = True

        # Проверяем что сумма процентов = 100
        total_traffic = sum(v.traffic_percentage for v in variants)
        if abs(total_traffic - 100.0) > 0.01:
            logger.warning(
                f"Сумма трафика не равна 100%: {total_traffic}%. "
                f"Нормализуем автоматически."
            )
            # Нормализуем
            for variant in self.variants:
                variant.traffic_percentage = (
                    variant.traffic_percentage / total_traffic * 100
                )

        logger.info(f"A/B тест создан: {test_id}, вариантов: {len(variants)}")

    def select_variant(self, user_id: Optional[str] = None) -> TestVariant:
        """
        Выбрать вариант для пользователя на основе распределения трафика.

        Args:
            user_id: ID пользователя (для консистентности)

        Returns:
            Выбранный вариант
        """
        if not self._is_active:
            logger.warning("Тест не активен, возвращаем первый вариант")
            return self.variants[0]

        # Используем user_id для детерминированного выбора (если есть)
        if user_id:
            hash_value = int(hashlib.md5(f"{self.test_id}-{user_id}".encode()).hexdigest(), 16)
            normalized = hash_value % 100
        else:
            normalized = random.uniform(0, 100)

        # Выбираем вариант на основе процентов
        cumulative = 0
        for variant in self.variants:
            cumulative += variant.traffic_percentage
            if normalized <= cumulative:
                return variant

        # Fallback
        return self.variants[-1]

    def record_result(
        self,
        variant_id: str,
        metrics: Dict[str, float],
        user_id: Optional[str] = None
    ):
        """
        Записать результат для варианта.

        Args:
            variant_id: ID варианта
            metrics: Измеренные метрики
            user_id: ID пользователя
        """
        if variant_id not in self._results:
            self._results[variant_id] = ABTestResult(
                test_id=self.test_id,
                variant_id=variant_id
            )

        result = self._results[variant_id]
        result.samples += 1

        # Обновляем средние метрики
        for metric_name, value in metrics.items():
            if metric_name not in result.metrics:
                result.metrics[metric_name] = value
            else:
                # Скользящее среднее
                old_avg = result.metrics[metric_name]
                result.metrics[metric_name] = (
                    (old_avg * (result.samples - 1) + value) / result.samples
                )

        logger.debug(
            f"Результат записан: {variant_id}, "
            f"выборок: {result.samples}"
        )

    def get_results(self) -> Dict[str, ABTestResult]:
        """Получить результаты теста"""
        return self._results

    def get_winner(self, metric: str) -> Optional[str]:
        """
        Определить победителя по метрике.

        Args:
            metric: Название метрики

        Returns:
            ID варианта-победителя
        """
        if not self._results:
            return None

        best_variant = None
        best_value = float('-inf')

        for variant_id, result in self._results.items():
            value = result.metrics.get(metric, 0.0)
            if value > best_value:
                best_value = value
                best_variant = variant_id

        return best_variant

    def is_statistically_significant(
        self,
        metric: str,
        confidence_level: float = 0.95
    ) -> bool:
        """
        Проверить статистическую значимость.

        Args:
            metric: Название метрики
            confidence_level: Уровень доверия

        Returns:
            True если результат статистически значим
        """
        # Упрощенная проверка (в продакшене использовать t-test или chi-square)
        if len(self._results) < 2:
            return False

        # Проверяем что у всех вариантов достаточно выборок
        for result in self._results.values():
            if result.samples < 30:  # Минимальный размер выборки
                return False

        # Проверяем что есть заметная разница между вариантами
        values = [
            result.metrics.get(metric, 0.0)
            for result in self._results.values()
        ]

        if not values:
            return False

        max_diff = max(values) - min(values)
        
        # Эвристика: разница должна быть > 5%
        return max_diff > 0.05

    def stop(self):
        """Остановить тест"""
        self._is_active = False
        self._end_time = datetime.utcnow()
        logger.info(f"A/B тест остановлен: {self.test_id}")

    def save_to_file(self) -> Path:
        """Сохранить тест на диск"""
        output_file = self._storage_path / f"{self.test_id}.json"

        data = {
            "test_id": self.test_id,
            "name": self.name,
            "test_type": self.test_type.value,
            "variants": [v.model_dump() for v in self.variants],
            "results": {
                vid: result.model_dump()
                for vid, result in self._results.items()
            },
            "start_time": self._start_time.isoformat(),
            "end_time": self._end_time.isoformat() if self._end_time else None,
            "is_active": self._is_active
        }

        # Преобразуем datetime в строки
        for variant in data["variants"]:
            if isinstance(variant.get("created_at"), datetime):
                variant["created_at"] = variant["created_at"].isoformat()

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"A/B тест сохранен: {output_file}")
        return output_file

    @classmethod
    def load_from_file(cls, file_path: Path) -> 'ABTest':
        """Загрузить тест из файла"""
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        variants = [TestVariant(**v) for v in data["variants"]]
        
        test = cls(
            test_id=data["test_id"],
            name=data["name"],
            test_type=VariantType(data["test_type"]),
            variants=variants
        )

        test._start_time = datetime.fromisoformat(data["start_time"])
        if data.get("end_time"):
            test._end_time = datetime.fromisoformat(data["end_time"])
        test._is_active = data["is_active"]

        logger.info(f"A/B тест загружен: {file_path}")
        return test


class ABTestManager:
    """
    Менеджер A/B тестов.

    Управляет несколькими тестами, собирает агрегированную статистику.
    """

    def __init__(self, storage_path: Optional[Path] = None):
        """Инициализация менеджера"""
        self._tests: Dict[str, ABTest] = {}
        self._storage_path = storage_path or Path("/app/data/ab_tests")
        try:
            self._storage_path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            logger.warning("Не могу создать /app/data, использую /tmp для ab_tests")
            self._storage_path = Path("/tmp/kag_ab_tests")
            self._storage_path.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"ABTestManager инициализирован")

    def create_test(
        self,
        test_id: str,
        name: str,
        test_type: VariantType,
        variants: List[TestVariant]
    ) -> ABTest:
        """
        Создать новый A/B тест.

        Args:
            test_id: ID теста
            name: Название
            test_type: Тип
            variants: Варианты

        Returns:
            Созданный тест
        """
        test = ABTest(
            test_id=test_id,
            name=name,
            test_type=test_type,
            variants=variants,
            storage_path=self._storage_path
        )

        self._tests[test_id] = test
        logger.info(f"A/B тест создан: {test_id}")
        return test

    def get_test(self, test_id: str) -> Optional[ABTest]:
        """Получить тест по ID"""
        return self._tests.get(test_id)

    def get_active_tests(self) -> List[ABTest]:
        """Получить все активные тесты"""
        return [t for t in self._tests.values() if t._is_active]

    def stop_test(self, test_id: str) -> bool:
        """Остановить тест"""
        test = self._tests.get(test_id)
        if test:
            test.stop()
            return True
        return False

    def generate_report(self) -> Dict[str, Any]:
        """Сгенерировать отчет по всем тестам"""
        report = {
            "total_tests": len(self._tests),
            "active_tests": len(self.get_active_tests()),
            "tests": []
        }

        for test_id, test in self._tests.items():
            test_summary = {
                "test_id": test_id,
                "name": test.name,
                "type": test.test_type.value,
                "variants_count": len(test.variants),
                "is_active": test._is_active,
                "results": {
                    vid: {
                        "samples": result.samples,
                        "metrics": result.metrics
                    }
                    for vid, result in test._results.items()
                }
            }
            report["tests"].append(test_summary)

        return report

    def save_all(self):
        """Сохранить все тесты на диск"""
        for test in self._tests.values():
            test.save_to_file()
        logger.info(f"Все тесты сохранены: {len(self._tests)} тестов")


# Глобальный менеджер
ab_test_manager = ABTestManager()
