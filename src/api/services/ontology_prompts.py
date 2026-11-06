"""Промпты извлечения онтологии: заводской текст, хранение в БД, история версий.

Зачем отдельный сервис, а не константа в коде: текст задания управляет тем,
что вообще попадёт в граф. Правка должна делаться без пересборки образа, но
с журналом — иначе через месяц никто не скажет, почему модель перестала
находить связи, и не будет к чему откатиться.

Модель хранения:
  ontology_prompts          текущая версия (одна строка на имя промпта);
  ontology_prompt_history   все прошлые версии, уникальны по (name, version).

Откат = сохранить текст старой версии как новую: история не переписывается,
и видно, кто и когда вернул.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

ENTITY_PROMPT = "entity_extraction"

# Заводской текст: то, что владелец сформулировал для извлечения сущностей в
# TypeDB. Восстановление этого текста — кнопка «Сбросить» на странице.
DEFAULT_ENTITY_PROMPT = """Ты — экспертный ИИ-модуль для извлечения онтологических данных из нормативных документов регуляторов РФ в сфере информационной безопасности (ФСТЭК, ФСБ) и безопасности ЗОКИИ (187-ФЗ, Приказы 235, 239 и др.) для графа знаний TypeDB.
Твоя задача — проанализировать поданный текст (чанк) и извлечь из него сущности, их атрибуты и многосторонние связи на основе предоставленной схемы.

### ПРАВИЛА ИЗВЛЕЧЕНИЯ:
1. Идентификация (ID): Для каждого извлеченного элемента сгенерируй логичный строковый ID (например, "fstek_order_239", "measure_ia_1", "cii_object_class"). Этот ID нужен ТОЛЬКО для связывания элементов внутри возвращаемого JSON.
2. Сущности (Entities): Извлекай только типы, перечисленные в схеме. Не придумывай новые типы.
3. Атрибуты (Attributes): Заполняй строго по схеме. Если атрибута нет в тексте, не выдумывай его, просто опусти поле в JSON.
4. Связи (Relations): Помни, что в TypeDB связи являются N-арными (многосторонними). Связь — это факт или контекст, объединяющий участников. Для каждой связи укажи ее тип (relation_type) и массив участников (participants), где каждый имеет роль (role) и ссылается на ID сущности (target_id).

### ДОСТУПНАЯ СХЕМА (TBox):
- Сущности (Entities):
  * regulator (Атрибуты: name, short_name) -> Роли: issuer
    (Пример: {"name": "Федеральная служба по техническому и экспортному контролю", "short_name": "ФСТЭК России"})
  * regulatory_document (Атрибуты: number, title, type) -> Роли: source_document, regulated_item
    (Пример: {"number": "239", "title": "Об утверждении Требований по обеспечению безопасности ЗОКИИ...", "type": "Приказ"})
  * structural_element (Атрибуты: index, text_content) -> Роли: structural_part, sub_part, source_requirement
    (Пример: {"index": "Пункт 12.1", "text_content": "...должно быть обеспечено управление конфигурацией..."})
  * security_measure (Атрибуты: code, description) -> Роли: core_measure, alternative_measure, applied_requirement
    (Пример: {"code": "ИА.1", "description": "Идентификация и аутентификация субъектов доступа..."})
  * cii_object (Атрибуты: category, system_type) -> Роли: target_system
    (Пример: {"category": "1 категория значимости", "system_type": "АСУ ТП"} или {"category": "любая", "system_type": "КИИ"})
  * threat_source (Атрибуты: offender_class, potential) -> Роли: threat_actor
    (Пример: {"offender_class": "Внешний нарушитель с базовым потенциалом"})

- Связи (Relations) и их роли:
  * document_issuance -> роли: [issuer, regulated_item]
    (Связь: Регулятор издал нормативный документ)
  * document_hierarchy -> роли: [structural_part, sub_part]
    (Связь: Вложенность элементов нормативного акта, например, подпункт входит в пункт)
  * measure_definition -> роли: [source_requirement, core_measure]
    (Связь: Пункт или приложение документа жестко закрепляет/описывает конкретную базовую меру ИБ)
  * applicability_criteria -> роли: [applied_requirement, target_system]
    (Связь: Требование или мера ИБ применяется к конкретному типу или категории объекта КИИ. Пример: мера обязательна для 1 и 2 категорий)
  * compensatory_relation -> роли: [core_measure, alternative_measure]
    (Связь: Возможность замены базовой меры ИБ компенсирующей мерой при невозможности ее технической реализации)

### ФОРМАТ ВЫХОДА (OUTPUT FORMAT):
Верни исключительно чистый JSON-объект без markdown-разметки (без ```json ... ```), без комментариев и преамбул. Если в тексте нет подходящих сущностей, верни пустые массивы.

{
  "entities": [
    {
      "id": "строка_уникальный_id",
      "type": "тип_сущности_из_схемы",
      "attributes": {
        "имя_атрибута": "значение"
      }
    }
  ],
  "relations": [
    {
      "id": "строка_уникальный_id",
      "type": "тип_связи_из_схемы",
      "attributes": {},
      "participants": [
        { "role": "роль_из_схемы", "target_id": "id_сущности_участника" }
      ]
    }
  ]
}

### ВХОДНОЙ ТЕКСТ ДЛЯ АНАЛИЗА:
{text_chunk}
"""

DEFAULTS = {ENTITY_PROMPT: DEFAULT_ENTITY_PROMPT}

# Что произойдёт после сохранения — показываем прямо на странице, а не в
# документации: иначе правку делают, не понимая её цены.
CONSEQUENCES = [
    "Промпт читается при каждом запуске извлечения: следующий прогон пойдёт уже по новому тексту.",
    "Уже извлечённые сущности и связи в TypeDB не меняются — чтобы применить новый текст "
    "к корпусу, документы нужно обработать заново.",
    "Плейсхолдер {text_chunk} обязателен: без него модель не получит текст фрагмента.",
    "История версий сохраняется: любую прошлую версию можно вернуть, правки не теряются.",
]


_DDL = [
    """
    CREATE TABLE IF NOT EXISTS ontology_prompts (
        name       text PRIMARY KEY,
        content    text NOT NULL,
        version    integer NOT NULL DEFAULT 1,
        updated_by text,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ontology_prompt_history (
        id         bigserial PRIMARY KEY,
        name       text NOT NULL,
        version    integer NOT NULL,
        content    text NOT NULL,
        updated_by text,
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (name, version)
    )
    """,
]


def validate_prompt(content: str) -> list[str]:
    """Проверки, которые ловят поломку до записи, а не на следующем прогоне."""
    problems: list[str] = []
    if not (content or "").strip():
        problems.append("Текст пуст.")
        return problems
    if "{text_chunk}" not in content:
        problems.append(
            "Нет плейсхолдера {text_chunk} — модель не увидит текст фрагмента "
            "и будет извлекать сущности из воздуха."
        )
    if len(content) < 400:
        problems.append("Текст короче 400 знаков: схема и правила почти наверняка потеряны.")
    return problems


class PromptStore:
    """Хранилище промптов в той же БД, что очередь предложений."""

    def __init__(self, db_url: str | None = None) -> None:
        self._db_url = db_url
        self._engine: Engine | None = None
        self._ready = False

    def _eng(self) -> Engine:
        if self._engine is None:
            url = self._db_url or os.environ.get("KAG_DB_URL", "")
            if not url:
                raise RuntimeError("KAG_DB_URL не задан — промпты недоступны")
            self._engine = create_engine(url, pool_pre_ping=True, pool_size=2, future=True)
        return self._engine

    def ensure_tables(self) -> None:
        if self._ready:
            return
        with self._eng().begin() as conn:
            for stmt in _DDL:
                conn.execute(text(stmt))
        self._ready = True

    # ── чтение ───────────────────────────────────────────────────────────
    def get(self, name: str = ENTITY_PROMPT) -> dict:
        """Текущий текст: из БД, а если строки нет — заводской."""
        self.ensure_tables()
        with self._eng().connect() as conn:
            row = conn.execute(
                text(
                    "SELECT content, version, updated_by, updated_at "
                    "FROM ontology_prompts WHERE name=:n"
                ),
                {"n": name},
            ).fetchone()

        if row:
            content, version, updated_by, updated_at = row
            source = "db"
        else:
            content, version, updated_by, updated_at = DEFAULTS[name], 1, None, None
            source = "default"

        return {
            "name": name,
            "content": content,
            "version": version,
            "updated_by": updated_by,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "source": source,
            "default_content": DEFAULTS[name],
            "is_modified": content != DEFAULTS[name],
            "consequences": CONSEQUENCES,
            "problems": validate_prompt(content),
        }

    # ── запись ───────────────────────────────────────────────────────────
    def save(self, name: str, content: str, updated_by: str) -> dict:
        """Записать новую версию. Старая уходит в историю, номер версии растёт."""
        problems = validate_prompt(content)
        if problems:
            return {"status": "error", "problems": problems}

        self.ensure_tables()
        with self._eng().begin() as conn:
            row = conn.execute(
                text("SELECT content, version FROM ontology_prompts WHERE name=:n"),
                {"n": name},
            ).fetchone()

            if row and row[0] == content:
                return {"status": "ok", "unchanged": True, "version": row[1]}

            next_version = (row[1] if row else 0) + 1
            if row:
                conn.execute(
                    text(
                        "INSERT INTO ontology_prompt_history (name, version, content, updated_by) "
                        "VALUES (:n, :v, :c, :by) ON CONFLICT (name, version) DO NOTHING"
                    ),
                    {"n": name, "v": row[1], "c": row[0], "by": updated_by},
                )
            conn.execute(
                text(
                    "INSERT INTO ontology_prompts (name, content, version, updated_by, updated_at) "
                    "VALUES (:n, :c, :v, :by, now()) "
                    "ON CONFLICT (name) DO UPDATE SET content=EXCLUDED.content, "
                    "version=EXCLUDED.version, updated_by=EXCLUDED.updated_by, updated_at=now()"
                ),
                {"n": name, "c": content, "v": next_version, "by": updated_by},
            )

        logger.info("промпт %s обновлён до версии %s (%s)", name, next_version, updated_by)
        return {"status": "ok", "version": next_version, "problems": []}

    def reset(self, name: str, updated_by: str) -> dict:
        """Вернуть заводской текст. Обычное сохранение — история сохраняется."""
        return self.save(name, DEFAULTS[name], updated_by)

    def history(self, name: str = ENTITY_PROMPT, limit: int = 20) -> list[dict]:
        """Прошлые версии, свежие сверху."""
        self.ensure_tables()
        with self._eng().connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT version, length(content), updated_by, updated_at "
                    "FROM ontology_prompt_history WHERE name=:n "
                    "ORDER BY version DESC LIMIT :lim"
                ),
                {"n": name, "lim": limit},
            ).fetchall()
        return [
            {
                "version": v,
                "chars": n,
                "updated_by": by,
                "updated_at": at.isoformat() if at else None,
            }
            for v, n, by, at in rows
        ]

    def history_version(self, name: str, version: int) -> str | None:
        self.ensure_tables()
        with self._eng().connect() as conn:
            row = conn.execute(
                text("SELECT content FROM ontology_prompt_history WHERE name=:n AND version=:v"),
                {"n": name, "v": version},
            ).fetchone()
        return row[0] if row else None

    def render(self, name: str, text_chunk: str) -> str:
        """Подставить фрагмент в текущий промпт — то, что реально увидит модель."""
        return self.get(name)["content"].replace("{text_chunk}", text_chunk)


prompt_store = PromptStore()
