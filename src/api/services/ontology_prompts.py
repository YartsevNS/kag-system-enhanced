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
DEFAULT_ENTITY_PROMPT = """Ты — экспертный ИИ-модуль для извлечения онтологических данных из нормативных документов регуляторов РФ в сфере информационной безопасности (ФСТЭК, ФСБ) и безопасности ЗОКИИ (187-ФЗ, Приказы 235, 239 и др.), а также из документов компании (политики, инструкции, регламенты) для графа знаний TypeDB.
Твоя задача — проанализировать поданный текст (чанк) и извлечь из него сущности, их атрибуты и многосторонние связи СТРОГО по предоставленной схеме.

### ПРАВИЛА ИЗВЛЕЧЕНИЯ:
1. Идентификация (ID): для каждого извлечённого элемента сгенерируй логичный строковый ID (например, "fstek_order_239", "measure_ia_1", "cii_object_class"). Этот ID нужен ТОЛЬКО для связывания элементов внутри возвращаемого JSON.
2. Сущности: извлекай только типы, перечисленные в схеме. Новые типы не придумывай.
3. Атрибуты: заполняй строго по схеме. Если атрибута нет в тексте — не выдумывай, опусти поле.
4. Связи: в TypeDB связи N-арные. Связь — это факт или контекст, объединяющий участников. Для каждой связи укажи тип (relation_type) и массив участников (participants), где каждый имеет роль (role) и ссылку на ID сущности (target_id).
5. ОБЯЗАТЕЛЬНЫЕ АТРИБУТЫ (без них база отвергнет запись — это жёсткое ограничение схемы, а не пожелание. ent-key должен быть глобально уникальным: используй префикс типа ("law:", "doc:", "asset:")):
   - ent-key — у КАЖДОЙ сущности;
   - title и document_key — у любого документа (акт и редакция различаются, см. п. 6);
   - requirement_key и requirement_kind — у требования; requirement_key = document_key + "|" + номер пункта (например "фстэк|приказ|239|п.11.1"), глобально уникален;
   - measure_key — у меры: document_key + "|" + код меры (например "фстэк|приказ|239|ИА.1");
   - version_string — у версии документа;
   - name — у объекта КИИ и у средства защиты;
   - cert_number — у сертификата.
   Если обязательного значения в тексте нет — не создавай сущность, а не подставляй догадку.
6. АКТ И РЕДАКЦИЯ — РАЗНЫЕ СУЩНОСТИ:
   - regulatory_document / company_document — сам акт, его идентичность. Атрибуты: title, doc_type, document_key (стабильный ключ вида "издатель|тип|номер"), number.
   - document_version — конкретная редакция/файл: version_string, effective_from, effective_to, version_status (current/superseded), kag_document_id.
   - ВСЕ редакции документа связываются связью document_has_version (document -> version);
   - действующая редакция дополнительно связывается current_version_of (document -> version);
   - замена редакции — supersedes (newer -> older). У документа может быть только одна текущая редакция: не создавай вторую.
   В тексте одного документа обычно одна редакция: создавай акт и одну версию, не дублируй акт на каждый чанк.
7. ТРАССИРОВКА — ТОЛЬКО СВЯЗЯМИ, строки source-* не заменяют связи:
   - каждый structural_element (пункт, подпункт, приложение) привязывай к версии: document_structure(version, element);
   - вложенность пунктов: document_hierarchy(parent, child);
   - каждое security_requirement выводи из пункта: requirement_derived_from(requirement, clause) — clause это structural_element, НЕ строка с номером пункта;
   - если в тексте мера реализует требование — requirement_definition(requirement, measure);
   - если мера описана в пункте (приложение, перечень) — measure_source(measure, clause);
   - применимость требования к объекту КИИ — requirement_applicability(requirement, target_system); применимость меры — measure_applicability(measure, target_system);
   - доказательство к требованию — requirement_evidence(requirement, evidence);
   - основание требования в документе — requirement_basis(requirement, source_document);
   - соответствие документа компании нормативному — document_compliance(company_doc, regulated_doc);
   - закрепление документа за объектом — document_assignment(regulated_doc, regulated_subject).
8. СЕРТИФИКАЦИЯ: если упомянут сертификат СЗИ — создавай certificate (cert_number, cert_authority, cert_until) и связывай certification(certified_software, certificate) и, если в тексте названо требование-основание, добавь basis_requirement (он необязателен). Не ограничивайся boolean is-certified, если есть номер/орган/срок.
9. УГРОЗЫ: threat_source связывай с объектами (threat_applicability) и с мерами (threat_measure), если текст это утверждает.
10. УСТАРЕВШЕЕ НЕ ИСПОЛЬЗУЙ: связи measure_definition, applicability_criteria с ролью applied_requirement, атрибуты current_version_string и current_version — исключены из схемы v3; у объекта КИИ категория хранится в cii-category (целое 1-3), поле category не заполняй. Если встретишь такие понятия в тексте, переводи их в новые связи по п. 7.
11. Не создавай объект «на будущее»: если в чанке неттипа, но есть только упоминание — не извлекай. Лучше меньше сущностей, но каждая с ключом и связями.

### ДОСТУПНАЯ СХЕМА (TBox v3):
- Сущности:
  * regulator (name, short_name) -> роль issuer. Пример: {"name": "Федеральная служба по техническому и экспортному контролю", "short_name": "ФСТЭК России"}
  * regulatory_document (title, doc_type, document_key, number) -> роли regulated_item, source_document, regulated_doc, company_doc. Пример: {"title": "Об утверждении Требований по обеспечению безопасности ЗОКИИ", "doc_type": "приказ", "document_key": "фстэк|приказ|239", "number": "239"}
  * company_document (title, doc_type, document_key) -> роли regulated_doc, company_doc, regulated_item. Пример: {"title": "Политика информационной безопасности", "doc_type": "политика", "document_key": "компания|политика|1"}
  * document_version (version_string, version_status, version_hash, effective_from, effective_to, kag_document_id) -> роли version, newer, older
  * structural_element (struct_index, text_content обязательно) -> роли element, parent, child, clause
  * security_requirement (requirement_key, requirement-id, requirement_kind, description, obligation-level) -> роли requirement
  * security_measure (measure_key, code, description) -> роли measure, core_measure, alternative_measure
  * evidence (name, evidence_kind, evidence_date) -> роль evidence
  * cii_object (name, cii-category, category, system_type) -> роли target_system, protected_system, regulated_subject
  * security_software (name, vendor, software_version, is-certified) -> роли deployed_szi, certified_software
  * certificate (cert_number, cert_authority, cert_until) -> роль certificate
  * threat_source (offender_class, potential) -> роли threat

- Связи и их роли:
  * document_issuance [issuer, regulated_item] — регулятор издал документ
  * current_version_of [document, version] — действующая редакция
  * supersedes [newer, older] — замена редакции
  * document_structure [version, element] — версия содержит пункт
  * document_hierarchy [parent, child] — вложенность пунктов
  * requirement_derived_from [requirement, clause] — требование выведено из пункта
  * requirement_definition [requirement, measure] — мера реализует требование
  * measure_source [measure, clause] — мера описана в пункте
  * compensatory_relation [core_measure, alternative_measure] — компенсирующая мера
  * requirement_applicability [requirement, target_system] — требование применимо к объекту
  * measure_applicability [measure, target_system] — мера применима к объекту
  * requirement_evidence [requirement, evidence] — доказательство выполнения
  * requirement_basis [requirement, source_document] — основание требования
  * document_compliance [company_doc, regulated_doc] — документ компании соответствует нормативному
  * document_assignment [regulated_doc, regulated_subject] — документ закреплён за объектом
  * asset_protection [protected_system, deployed_szi] — объект защищён СЗИ
  * certification [certified_software, certificate, basis_requirement] — сертификат СЗИ
  * threat_applicability [threat, target_system] — угроза применима к объекту
  * threat_measure [threat, measure] — угроза закрывается мерой

### ФОРМАТ ВЫХОДА (OUTPUT FORMAT):
Верни исключительно чистый JSON-объект без markdown-разметки (без ```json ... ```), без комментариев и преамбул. Если в тексте нет подходящих сущностей — верни пустые массивы.

{
  "entities": [
    {"id": "строка_уникальный_id", "type": "тип_сущности_из_схемы", "attributes": {"имя_атрибута": "значение"}}
  ],
  "relations": [
    {"type": "тип_связи", "participants": [{"role": "роль", "target_id": "id_сущности"}]}
  ]
}

### ПЕРЕД ВЫВОДОМ ПРОВЕРЬ СЕБЯ:
- у каждой сущности есть ent-key;
- у документа — title и document_key; у требования — requirement_key, requirement-id, requirement_kind; у версии — version_string; у объекта КИИ и СЗИ — name; у сертификата — cert_number;
- каждый structural_element связан с версией через document_structure;
- каждое требование связано с пунктом через requirement_derived_from;
- ни один тип и ни одна роль не выдуманы: сверься со списком выше.

### ТЕКСТ ФРАГМЕНТА ДЛЯ АНАЛИЗА:
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
