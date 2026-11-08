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
DEFAULT_ENTITY_PROMPT = """Ты — модуль извлечения онтологических фактов из документов по информационной безопасности РФ (ФСТЭК, ФСБ, ГОСТ, Банк России) и документов компании. Возвращай только JSON.

### СХЕМА (единственный источник типов, атрибутов и ролей; звёздочка — обязательное поле):
{schema_tbox}

### ПРАВИЛА:
1. id в JSON = ent-key. Формат "тип:ключ", глобально уникальный: regulator:fstek, doc:fstek|приказ|239, measure:fstek|239|ia-1. Один идентификатор на сущность; второй не генерируй.
2. Бери только типы, атрибуты и роли из схемы. Новые не придумывай.
3. Обязательное (@key, @card(1..1)) отсутствует в тексте — сущность не создавай. Опциональное отсутствует — опусти поле. Это правило сильнее правила 2.
4. Даты — строка ISO 8601 ("2026-11-08"). cii-category — целое 1..3. Числа — числами.
5. Акт и редакция — разные сущности: документ (title, document_key) и document_version (version_string). Все редакции связывай document_has_version; действующую — дополнительно current_version_of, и она у документа одна. Ключи: document_key = "издатель|тип|номер"; у требования requirement_key = document_key + "|" + номер пункта; у меры measure_key = document_key + "|" + код меры. Оба — @key, их обязательно заполнять.
6. Трассировка — связями: document_structure (версия → пункт), requirement_derived_from (требование → пункт), requirement_definition (требование → мера), measure_source (мера → пункт), а для документа компании — requirement_coverage (какое требование он покрывает). Строки source-* — денормализация, они не заменяют связи.
7. Связи бинарные, кроме certification (три участника: certified_software, certificate, basis_requirement). basis_requirement указывай только при явном основании в тексте.
8. Сертификат: есть номер — создавай certificate (cert_number обязателен, орган и срок — если есть) и связывай certification. Без номера certificate не создавай. Поле is-certified у средства защиты НЕ заполняй: источник правды — объект certificate.
9. requirement_applicability — только к конкретному объекту КИИ. Если сказано «для объектов 1 категории», объект не выдумывай: оставь категорию в description требования.
10. Если в контексте уже передан document_key, акт повторно не создавай — добавляй только новые связи и редакции.
11. Не извлекай по простому упоминанию: нужна утверждающая формулировка. Нет данных — пустые массивы.

### ПРИМЕР (фрагмент → JSON):
Текст: «3.1. Средства защиты информации, применяемые на значимых объектах критической информационной инфраструктуры, должны иметь сертификат соответствия.»
Выход:
{"entities":[{"id":"doc:fstek|приказ|239","type":"regulatory_document","attributes":{"title":"Требования по обеспечению безопасности значимых объектов КИИ","doc_type":"приказ","document_key":"фстэк|приказ|239","number":"239"}},{"id":"version:fstek|239|ред-1","type":"document_version","attributes":{"version_string":"1","kag_document_id":"kag-239"}},{"id":"element:fstek|239|3.1","type":"structural_element","attributes":{"struct_index":"3.1","text_content":"Средства защиты информации, применяемые на значимых объектах КИИ, должны иметь сертификат соответствия."}},{"id":"law:fstek|239|3.1","type":"security_requirement","attributes":{"requirement_key":"фстэк|приказ|239|3.1","requirement-id":"3.1","requirement_kind":"certification","description":"применять сертифицированные СЗИ"}}],"relations":[{"type":"document_has_version","participants":[{"role":"document","target_id":"doc:fstek|приказ|239"},{"role":"version","target_id":"version:fstek|239|ред-1"}]},{"type":"document_structure","participants":[{"role":"version","target_id":"version:fstek|239|ред-1"},{"role":"element","target_id":"element:fstek|239|3.1"}]},{"type":"requirement_derived_from","participants":[{"role":"requirement","target_id":"law:fstek|239|3.1"},{"role":"clause","target_id":"element:fstek|239|3.1"}]}]}

### ФОРМАТ ВЫХОДА:
Только JSON без markdown и комментариев: {"entities":[{"id","type","attributes"}],"relations":[{"type","participants":[{"role","target_id"}]}]}

### ТЕКСТ ФРАГМЕНТА:
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
    if "{schema_tbox}" not in content:
        problems.append(
            "Нет плейсхолдера {schema_tbox} — схема не подставляется из базы, "
            "промпт и онтология разойдутся."
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
        """Подставить фрагмент и живой TBox в текущий промпт — то, что увидит модель.

        {schema_tbox} берётся из фактической схемы TypeDB, а не из текста промпта:
        иначе промпт и схема расходятся (именно так в промпте осталась роль
        source_requirement, которой в схеме v3 нет).
        """
        template = self.get(name)["content"]
        if "{schema_tbox}" in template:
            tbox = ""
            try:
                from src.api.services.ontology_service import ontology_service

                tbox = ontology_service.tbox_summary()
            except Exception as exc:  # noqa: BLE001 — без TBox промпт бесполезен, но не роняем извлечение
                logger.warning("TBox для промпта не собран: %s", str(exc)[:160])
            template = template.replace("{schema_tbox}", tbox or "(схема недоступна)")
        return template.replace("{text_chunk}", text_chunk)


prompt_store = PromptStore()
