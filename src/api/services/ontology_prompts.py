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
DEFAULT_ENTITY_PROMPT = """Ты — экспертный ИИ-модуль для извлечения онтологических данных из документов по информационной безопасности: нормативных документов регуляторов РФ (ФСТЭК, ФСБ, Банк России), национальных стандартов и ГОСТов, а также внутренних документов организации (политики, регламенты, инструкции, стандарты).
Твоя задача — проанализировать фрагмент текста и извлечь из него сущности, их атрибуты и связи строго по приведённой схеме для графа знаний TypeDB.

### ПРАВИЛА ИЗВЛЕЧЕНИЯ
1. Идентификатор (id) придумывается ТОЛЬКО для связывания сущностей внутри этого ответа. В базу он не попадает.
2. Извлекай только типы, перечисленные в схеме. Новые типы не придумывай.
3. Атрибуты заполняй строго по схеме и только значениями из текста. Нет значения в тексте — поле просто не указывай. Ничего не додумывай.
4. Связи связывают только сущности из этого же ответа. Для каждой связи укажи тип, роль и target_id каждого участника. Связь без всех обязательных участников не выводи.
5. Если в тексте нет ни одного требования, сущности или связи из схемы — верни пустые массивы. Пустой ответ лучше выдуманного.

### КЛЮЧ ТРЕБОВАНИЯ (requirement-id) — ОБЯЗАТЕЛЬНО
Требование идентифицируется устойчивым ключом «ИСТОЧНИК:пункт»:
- источник — короткое обозначение документа: «ГОСТ-57580.1», «ФСТЭК-239», «ФСТЭК-235», «ФЗ-187», «ЦБ-57580», «ISO-27001»;
- пункт — номер пункта/раздела/строки таблицы мер ровно как в тексте: «п.5.2.1», «п.11.1», «ЗИС.2», «УЗ-2.1».
Примеры: «ГОСТ-57580.1:п.5.2.1», «ФСТЭК-239:п.11.1», «ФСТЭК-235:ЗИС.2».
Если номер пункта в тексте отсутствует, но фрагмент начинается с номера в метаданных — бери его оттуда. Если номера нет нигде — НЕ выдумывай: такое требование не извлекай.
Одно и то же требование, упомянутое дважды, — это одна сущность с одним ключом.

### УРОВЕНЬ ОБЯЗАТЕЛЬНОСТИ (obligation-level)
Только два значения, строго по формулировке текста:
- «обязательно» — если в тексте «должен», «должны», «обязан», «необходимо», «не допускается», «запрещается»;
- «рекомендуется» — если «следует», «рекомендуется», «целесообразно», «может».
Если формулировка нейтральная — поле не указывай.

### ДОСТУПНАЯ СХЕМА (TBox)
Сущности и их атрибуты:
* regulation-source — нормативный источник: закон, приказ, ГОСТ, стандарт, ISO/NIST.
  Атрибуты: name (полное обозначение, например «ГОСТ Р 57580.1-2017»), version (год или редакция). Роли: source.
* security-requirement — требование регулятора или стандарта.
  Атрибуты: requirement-id (ключ по правилу выше), description (ДОСЛОВНАЯ формулировка требования из текста, без пересказа), obligation-level, source-clause (номер пункта как в тексте).
  Дополнительные параметры — только если прямо названы: password-length (целое), change-period (целое, дней), char-composition (строка).
  Роли: requirement, stricter, weaker, supplement, original, conflicting, conflicted.
* compliance-document — документ ИБ самой организации (политика, регламент, инструкция, стандарт).
  Атрибуты: title, doc-kind (стандарт|политика|регламент|инструкция), version. Роль: document.
* company-document — иной документ организации (устав, приказ, договор). Атрибуты: title, version. Роль: regulated-doc.
* evidence — доказательство исполнения требования: ОРД, скриншот, отчёт сканера, компенсирующая мера.
  Атрибуты: name, evidence-kind (ОРД|скриншот|отчёт-сканера|компенсирующая-мера). Роль: evidence.
* application-domain — область применения: ГИС, ИСПДн, КИИ, Финансы. Атрибут: name. Роль: domain.
* cii-object — объект критической информационной инфраструктуры. Атрибуты: name, cii-category (целое 1, 2 или 3). Роли: protected-system, regulated-subject.
* security-software — средство защиты информации. Атрибуты: name, is-certified (true/false, только если в тексте прямо сказано о сертификации). Роль: deployed-szi.

Связи и роли:
* requirement-generation — источник порождает требование: source (regulation-source) → requirement (security-requirement).
* requirement-regulation — требование регламентирует документ: requirement (security-requirement) → document (compliance-document или company-document).
* requirement-scope — требование применяется к области: requirement (security-requirement) → domain (application-domain).
* requirement-evidence — требование подтверждается доказательством: requirement (security-requirement) → evidence (evidence).
* requirement-relation — отношения между требованиями (только при явном указании в тексте: «ужесточает», «дополняет», «заменяет», «противоречит»):
  stricter ↔ weaker (ужесточает), supplement ↔ original (дополняет), conflicting ↔ conflicted (противоречит).
* asset-protection — СЗИ защищает объект КИИ: protected-system (cii-object) → deployed-szi (security-software).
* document-assignment — объект/субъект связан с документом: regulated-subject (cii-object) → regulated-doc (company-document).

### ЧЕГО НЕ ИЗВЛЕКАТЬ
- Общие рассуждения, определения, оглавления, колонтитулы, реквизиты страницы, ФИО исполнителей.
- Угрозы и уязвимости как отдельные сущности — в этой схеме их нет.
- Связи «упоминается рядом», «относится к теме» — только явные, названные в тексте связи.

### ФОРМАТ ВЫХОДА
Верни исключительно чистый JSON без markdown-обёртки, без комментариев и преамбулы. Если извлекать нечего — верни {"entities": [], "relations": []}.

{
  "entities": [
    {"id": "e1", "type": "security-requirement",
     "attributes": {"requirement-id": "ГОСТ-57580.1:п.5.2.1",
                    "description": "дословная формулировка требования",
                    "obligation-level": "обязательно",
                    "source-clause": "п.5.2.1"}}
  ],
  "relations": [
    {"type": "requirement-generation",
     "participants": [{"role": "source", "target_id": "e0"},
                      {"role": "requirement", "target_id": "e1"}]}
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

            # Заводской текст — это версия 1, даже если его ни разу не сохраняли.
            # Иначе первая же правка получила бы номер 1 и перекрылась бы с
            # заводской в истории: непонятно, что именно откатывать.
            next_version = (row[1] if row else 1) + 1
            if row:
                conn.execute(
                    text(
                        "INSERT INTO ontology_prompt_history (name, version, content, updated_by) "
                        "VALUES (:n, :v, :c, :by) ON CONFLICT (name, version) DO NOTHING"
                    ),
                    {"n": name, "v": row[1], "c": row[0], "by": updated_by},
                )
            elif content != DEFAULTS[name]:
                conn.execute(
                    text(
                        "INSERT INTO ontology_prompt_history (name, version, content, updated_by) "
                        "VALUES (:n, 1, :c, :by) ON CONFLICT (name, version) DO NOTHING"
                    ),
                    {"n": name, "c": DEFAULTS[name], "by": "заводской"},
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
