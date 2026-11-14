"""Локальная обёртка: сборка схемы из ядра и модулей.

Код живёт в src/ontology/build_schema.py (попадает в образ), здесь — только запуск из репозитория:
    python scripts/build_schema.py --db cybersecurity_ontology_v24
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ontology.build_schema import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
