"""
Process Logger — детальное логирование всех этапов обработки документа.

Сохраняет в config_store (process_logs) для каждого document_id:
- Временные метки каждого этапа
- Длительность каждого этапа (delta_ms) и время от старта (elapsed_ms)
- Параметры чанкинга (chunk_size, chunk_overlap)
- Количество чанков, векторов
- Результаты анализа (title, type, summary)
- Извлечённые сущности и связи
- Ошибки если есть
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
import time
from loguru import logger


class ProcessLogger:
    """Логгер процесса обработки документа."""

    def __init__(self, document_id: str):
        self.document_id = document_id
        self._log: List[Dict[str, Any]] = []
        # Точки отсчёта для расчёта длительности этапов.
        # time.monotonic() — монотонные часы: не сбиваются при NTP-коррекции
        # или переводе системного времени, поэтому разности надёжны.
        self._started = time.monotonic()
        self._last = time.monotonic()

    def log(self, step: str, details: Dict[str, Any] = None):
        """Записать шаг обработки.

        В каждый entry автоматически добавляются:
        - elapsed_ms — время от старта обработки (start) до этого шага
        - delta_ms   — сколько занял ТЕКУЩИЙ шаг (от предыдущего log() до этого)

        Это позволяет увидеть, на каком этапе документ «застревает»
        (OCR / чанкинг / векторизация / LLM-типизация), не гадая по общему времени.
        """
        now = time.monotonic()
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "step": step,
            "elapsed_ms": round((now - self._started) * 1000, 1),
            "delta_ms": round((now - self._last) * 1000, 1),
            "details": details or {}
        }
        self._last = now
        self._log.append(entry)
        logger.info(
            f"[{self.document_id[:8]}] {step} "
            f"(+{entry['delta_ms']}ms, total {entry['elapsed_ms']}ms): {details or ''}"
        )

    def log_error(self, step: str, error: str):
        """Записать ошибку."""
        self.log(step, {"error": str(error)})
        logger.error(f"[{self.document_id[:8]}] {step} ERROR: {error}")

    def save(self):
        """Сохранить лог в config_store."""
        try:
            from src.api.services.config_store import config_store
            key = f"log_{self.document_id}"
            existing = config_store.get("process_logs", key, [])
            existing.extend(self._log)
            config_store.set("process_logs", key, existing)
        except Exception as e:
            logger.warning(f"Не удалось сохранить лог процесса: {e}")

    def get_summary(self) -> Dict[str, Any]:
        """Краткая сводка обработки."""
        steps = [e["step"] for e in self._log]
        errors = [e for e in self._log if "error" in str(e.get("details", {}))]
        return {
            "document_id": self.document_id,
            "steps_count": len(self._log),
            "steps": steps,
            "errors": len(errors),
            "last_step": steps[-1] if steps else None,
            "duration_ms": self._calc_duration()
        }

    def _calc_duration(self) -> Optional[float]:
        if len(self._log) < 2:
            return None
        try:
            t1 = datetime.fromisoformat(self._log[0]["timestamp"])
            t2 = datetime.fromisoformat(self._log[-1]["timestamp"])
            return (t2 - t1).total_seconds() * 1000
        except Exception:
            return None


def get_process_log(document_id: str) -> List[Dict]:
    """Получить лог обработки документа."""
    try:
        from src.api.services.config_store import config_store
        return config_store.get("process_logs", f"log_{document_id}", [])
    except Exception:
        return []


def get_all_process_logs(limit: int = 50) -> List[Dict]:
    """Получить все логи обработки."""
    try:
        from src.api.services.config_store import config_store
        all_logs = config_store.get_all("process_logs")
        result = []
        for key, entries in all_logs.items():
            doc_id = key.replace("log_", "")
            if entries:
                first = entries[0]
                last = entries[-1]
                result.append({
                    "document_id": doc_id,
                    "started_at": first.get("timestamp"),
                    "finished_at": last.get("timestamp"),
                    "steps": len(entries),
                    "status": "completed" if any("completed" in e.get("step", "") for e in entries) else "processing"
                })
        result.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return result[:limit]
    except Exception:
        return []
