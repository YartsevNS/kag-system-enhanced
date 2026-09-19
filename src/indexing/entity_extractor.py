"""
Entity Extractor v2.0 — извлечение сущностей через LLM (Neo4j Best Practices).

Ключевые улучшения:
1. Двухпроходное извлечение: быстрый проход (ключевые сущности) + глубокий (связи)
2. Доменная схема: настраиваемые типы сущностей под предметную область
3. Не перегруженные промпты: один проход = одна группа типов сущностей
4. Валидация результатов: проверка качества перед сохранением
5. Контекст-менеджмент: правильный размер чанка для LLM
"""

from typing import Dict, Any, List, Optional
from loguru import logger
import json
import re
import asyncio

# Таймаут на одну синхронную Neo4j-операцию (create_entity/create_relation).
# Страховка от зависания: если Neo4j недоступен, вызов уходит в отдельный
# поток и через NEO4J_TIMEOUT отдаёт управление, не замораживая worker.
NEO4J_TIMEOUT = 20


# Действующая доменная схема хранится В НАСТРОЙКАХ: {"mode": "preset"|"manual",
# "preset": имя_или_None, "schema": {...}}. Память процесса обнуляется при
# рестарте, а worker — отдельный процесс, поэтому единственный источник правды
# здесь, а не в атрибутах класса.
DOMAIN_ACTIVE_KEY = "domain_schema_active"


def _load_active_domain_config():
    """Прочитать действующую доменную схему из config_store.

    Отдельная функция — чтобы подменять её в тестах (config_store тянет БД).
    """
    from src.api.services.config_store import config_store
    return config_store.get("kg_config", DOMAIN_ACTIVE_KEY)


class EntityExtractor:
    """Извлекает сущности и факты из чанков через LLM.
    
    Реализует итеративную стратегию Neo4j:
    - Pass 1: Извлечение КЛЮЧЕВЫХ сущностей (имена, названия, даты, суммы)
    - Pass 2: Извлечение СВЯЗЕЙ между сущностями
    - Опциональный Pass 3: Извлечение ДОПОЛНИТЕЛЬНЫХ типов сущностей
    
    Каждый проход использует ОТДЕЛЬНЫЙ промпт — не перегружаем LLM.
    """

    # ============================================================
    # Пресеты доменных схем (именованные конфигурации)
    # Каждый пресет — это словарь {core, relations, extended}
    # Переключение между пресетами меняет поведение извлечения сущностей
    # ============================================================
    SCHEMA_PRESETS = {
        "universal": {
            "name": "🌐 Универсальная",
            "description": "Подходит для любых документов. Извлекает базовые сущности: люди, организации, даты, суммы, места.",
            "use_cases": "Общее делопроизводство, смешанные архивы документов",
            "schema": {
                "core": {
                    "person": "Человек (ФИО, должность, роль)",
                    "organization": "Организация (компания, банк, госорган, учреждение)",
                    "date": "Дата (любого формата: ДД.ММ.ГГГГ, словесная, относительная)",
                    "money": "Денежная сумма с указанием валюты (руб, USD, EUR и др.)",
                },
                "relations": {
                    "SIGNED_BY": "Документ подписан человеком",
                    "BELONGS_TO": "Объект/счёт/документ принадлежит организации или человеку",
                    "DATED": "Событие/документ имеет дату",
                    "AMOUNT": "Операция на указанную сумму",
                    "LOCATED_AT": "Организация/человек находится по адресу",
                },
                "extended": {
                    "location": "Адрес, местонахождение (город, улица, индекс)",
                    "document_ref": "Ссылка на документ (номер, серия, тип документа)",
                    "legal_term": "Юридический термин, статья закона, нормативный акт",
                }
            }
        },
        "accounting": {
            "name": "💰 Бухгалтерия и финансы",
            "description": "Для счетов, квитанций, платёжек. Извлекает: плательщик, получатель, суммы, реквизиты.",
            "use_cases": "Бухгалтерские документы, банковские выписки, счета-фактуры, квитанции",
            "schema": {
                "core": {
                    "payer": "Плательщик (кто переводит деньги: ФИО или организация)",
                    "payee": "Получатель (кому переводят: ФИО или организация)",
                    "amount": "Сумма платежа с валютой",
                    "date": "Дата платежа/операции",
                    "bank_account": "Номер счёта или карты (20 цифр, 16 цифр)",
                    "bik": "БИК банка (9 цифр)",
                    "inn": "ИНН организации или физического лица (10 или 12 цифр)",
                },
                "relations": {
                    "PAID_BY": "Платёж совершён плательщиком",
                    "RECEIVED_BY": "Платёж получен получателем",
                    "FROM_ACCOUNT": "Списание со счёта",
                    "TO_ACCOUNT": "Зачисление на счёт",
                    "IN_BANK": "Операция в банке",
                },
                "extended": {
                    "invoice_number": "Номер счёта/квитанции/платёжки",
                    "purpose": "Назначение платежа",
                    "tax_amount": "Сумма налога (НДС и др.)",
                }
            }
        },
        "legal": {
            "name": "⚖️ Юридические документы",
            "description": "Для договоров, соглашений, актов. Извлекает: стороны, предмет, сроки, подписантов.",
            "use_cases": "Договоры, контракты, соглашения, доверенности, акты приёма-передачи",
            "schema": {
                "core": {
                    "contract_party": "Сторона договора (юрлицо или физлицо)",
                    "signer": "Подписант (ФИО, должность, на основании чего действует)",
                    "date": "Дата (подписания, вступления в силу, окончания)",
                    "contract_number": "Номер договора/соглашения",
                    "amount": "Сумма договора/сделки с валютой",
                    "jurisdiction": "Юрисдикция (город, страна, применимое право)",
                },
                "relations": {
                    "SIGNED_BY": "Документ подписан конкретным лицом",
                    "BINDS": "Договор обязывает сторону",
                    "AMENDS": "Документ изменяет/дополняет другой документ",
                    "TERMINATES": "Документ прекращает действие другого",
                    "REFERENCES": "Документ ссылается на другой документ или статью",
                },
                "extended": {
                    "legal_term": "Юридический термин (форс-мажор, неустойка, арбитраж)",
                    "clause": "Пункт/статья договора",
                    "validity_period": "Срок действия (с...по...)",
                }
            }
        },
        "medical": {
            "name": "🏥 Медицинские документы",
            "description": "Для медкарт, рецептов, заключений. Извлекает: пациент, врач, диагноз, лекарства.",
            "use_cases": "Медицинские карты, рецепты, заключения врачей, результаты анализов, выписки",
            "schema": {
                "core": {
                    "patient": "Пациент (ФИО, дата рождения, пол)",
                    "doctor": "Врач (ФИО, специальность, должность)",
                    "diagnosis": "Диагноз (код МКБ, название заболевания)",
                    "date": "Дата (осмотра, назначения, госпитализации)",
                    "medical_facility": "Медицинское учреждение (больница, поликлиника, клиника)",
                },
                "relations": {
                    "TREATED_BY": "Пациент лечится у врача",
                    "DIAGNOSED_WITH": "Пациенту поставлен диагноз",
                    "PRESCRIBED": "Врач назначил лекарство/процедуру",
                    "ADMITTED_TO": "Пациент госпитализирован в учреждение",
                },
                "extended": {
                    "medication": "Лекарственное средство (название, форма, дозировка)",
                    "lab_result": "Результат анализа (показатель + значение)",
                    "procedure": "Медицинская процедура или операция",
                }
            }
        },
        "infosec": {
            "name": "🔐 Информационная безопасность",
            "description": "Для документов ИБ: активы, уязвимости, угрозы, инциденты, меры защиты. Классификация по ГОСТ Р 56545/56546, ФСТЭК.",
            "use_cases": "Политики ИБ, отчёты аудита, карты рисков, планы защиты, реестры активов, журналы инцидентов",
            "schema": {
                "core": {
                    "asset": "Информационный актив (сервер, БД, приложение, канал связи, документ). Ключевой объект защиты.",
                    "vulnerability": "Уязвимость (CVE, слабость в ПО/процессе). Что может быть использовано для атаки.",
                    "threat": "Угроза (источник опасности: хакер, инсайдер, стихия). Кто/что может нанести ущерб.",
                    "incident": "Инцидент ИБ (факт нарушения: утечка, взлом, отказ). Что произошло.",
                    "control": "Мера защиты (межсетевой экран, шифрование, политика, СКЗИ). Средство противодействия.",
                    "date": "Дата (обнаружения, реагирования, аудита, окончания действия сертификата)",
                },
                "relations": {
                    "PROTECTS": "Мера защиты защищает актив от угрозы",
                    "EXPLOITS": "Угроза эксплуатирует уязвимость актива",
                    "CAUSED_BY": "Инцидент вызван уязвимостью",
                    "MITIGATES": "Мера защиты снижает (митигирует) риск",
                    "AFFECTS": "Инцидент затрагивает актив",
                    "REPORTED_BY": "Инцидент зарегистрирован сотрудником/системой",
                },
                "extended": {
                    "risk_level": "Уровень риска (низкий, средний, высокий, критический)",
                    "classification": "Гриф/категория (ДСП, КТ, гостайна, персональные данные)",
                    "regulation": "Нормативный документ (ФЗ-152, ГОСТ 57580, приказ ФСТЭК, PCI DSS)",
                    "security_clearance": "Уровень допуска (конфиденциально, секретно, СС, ОВ)",
                    "certificate": "Сертификат/аттестат соответствия (номер, срок действия, орган)",
                    "countermeasure": "Контрмера (конкретное действие по нейтрализации угрозы или устранению уязвимости)",
                }
            }
        },
    }

    # Активный пресет (по умолчанию — универсальный)
    _active_preset = "universal"

    # Ключ настроек, где лежит ДЕЙСТВУЮЩАЯ схема: {"mode": "preset"|"manual",
    # "preset": имя_или_None, "schema": {...}}. Единственный источник правды —
    # память процесса обнуляется при каждом рестарте.
    DOMAIN_ACTIVE_KEY = DOMAIN_ACTIVE_KEY  # единый ключ настроек (см. выше)

    # Доменная схема: группы типов для итеративного извлечения
    # Это — активная схема, получаемая из выбранного пресета
    DOMAIN_SCHEMA = SCHEMA_PRESETS["universal"]["schema"]

    def __init__(self):
        # Модель и URL НЕ хардкодим — берутся из admin (function_map:graph через
        # provider_service). Fallback на phi4-mini убран: если функция graph не
        # настроена в админке, извлечение сущностей просто пропускается.
        self._domain_config = dict(self.DOMAIN_SCHEMA)  # Копия, можно менять

    # ============================================================
    # Конфигурация
    # ============================================================

    def _get_graph_config(self):
        """Получить конфигурацию модели графа ТОЛЬКО из admin (function_map:graph).

        Раньше был fallback на config_store graph_model и _graph_model_config
        (хардкод phi4-mini:latest). Убран: если функция graph не настроена
        в админке — возвращаем None, извлечение пропускается (без phi4-mini).
        """
        try:
            from src.api.services.provider_service import provider_service
            cfg = provider_service.get_function_llm_config("graph")
            if cfg and cfg.get("model"):
                return cfg
        except Exception:
            pass
        return None

    def set_domain_schema(self, schema: Dict, mark_manual: bool = False):
        """Установить пользовательскую доменную схему (словарь).

        mark_manual=True помечает схему как ручную (пресет перестаёт считаться
        активным) — иначе интерфейс подсвечивал бы пресет, схема которого уже
        заменена руками.
        """
        self._domain_config = dict(schema)
        if mark_manual:
            EntityExtractor._active_preset = "manual"

    def apply_stored_domain_schema(self) -> str:
        """Применить сохранённую доменную схему (старт процесса/рестарт api или worker).

        Почему так: схема и пресет хранились ТОЛЬКО в памяти процесса. Настройку
        писали в config_store, но никто её не читал — после рестарта api пресет
        сбрасывался на «universal», а worker (в нём идёт извлечение сущностей)
        выбранный в админке пресет не видел вообще, т.к. это другой процесс.

        Возвращает режим: "preset:<имя>" | "manual" | "default".
        """
        try:
            saved = _load_active_domain_config() or {}
        except Exception as e:
            logger.warning(f"[domain] сохранённую доменную схему прочитать не удалось: {e}")
            return "default"
        if not isinstance(saved, dict) or not saved:
            return "default"

        if saved.get("mode") == "preset":
            name = saved.get("preset")
            if name in EntityExtractor.SCHEMA_PRESETS:
                EntityExtractor.switch_preset(name)
                self._domain_config = dict(EntityExtractor.SCHEMA_PRESETS[name]["schema"])
                logger.info(f"[domain] применён сохранённый пресет: {name}")
                return f"preset:{name}"
            logger.warning(f"[domain] в настройках неизвестный пресет {name!r} — оставляю текущую схему")
            return "default"

        schema = saved.get("schema")
        if saved.get("mode") == "manual" and isinstance(schema, dict) and schema:
            self.set_domain_schema(schema, mark_manual=True)
            logger.info(
                f"[domain] применена сохранённая ручная схема: "
                f"{len(schema.get('core', {}))} базовых типов"
            )
            return "manual"
        return "default"

    @classmethod
    def switch_preset(cls, preset_name: str) -> Dict:
        """Переключить активный пресет доменной схемы по имени.
        
        Args:
            preset_name: одно из: universal, accounting, legal, medical, infosec
        
        Returns:
            Словарь с новой схемой или ошибкой
        """
        if preset_name not in cls.SCHEMA_PRESETS:
            return {"error": f"Неизвестный пресет: {preset_name}. Доступны: {list(cls.SCHEMA_PRESETS.keys())}"}
        cls._active_preset = preset_name
        cls.DOMAIN_SCHEMA = cls.SCHEMA_PRESETS[preset_name]["schema"]
        # Сохраняем в config_store
        try:
            from src.api.services.config_store import config_store
            config_store.set("kg_config", "active_preset", preset_name)
        except Exception as e:
            pass  # не критично
        return {"status": "ok", "preset": preset_name, "name": cls.SCHEMA_PRESETS[preset_name]["name"]}

    # ── Кэш извлечения: версия ключа ────────────────────────────────────────
    # Ключ кэша = llm_<версия>_<хэш текста>. Без версии кэш не знал, ЧЕМ извлекали:
    # после смены модели, доменной схемы или промпта старые чанки возвращали прежний
    # результат молча (ложное «работает по-новому»). Версия считается из того, что
    # реально влияет на извлечение. Меняете ШАБЛОНЫ промптов в коде ниже — увеличьте
    # PROMPT_EPOCH, иначе кэш останется от старой логики.
    PROMPT_EPOCH = 1

    @classmethod
    def cache_version(cls, cfg: Optional[Dict[str, Any]] = None) -> str:
        """Короткая версия кэша извлечения (модель, промпт, схема, режим)."""
        import hashlib as _h
        import json as _json
        cfg = cfg or {}
        schema = getattr(cls, "DOMAIN_SCHEMA", None) or {}
        payload = "|".join([
            str(getattr(cls, "PROMPT_EPOCH", 1)),
            str(cfg.get("model") or ""),
            str(cfg.get("provider") or ""),
            str(cfg.get("extraction_mode") or ""),
            str(cfg.get("system_prompt") or "")[:2000],
            str(getattr(cls, "_active_preset", "")),
            _json.dumps(schema, ensure_ascii=False, sort_keys=True)[:5000],
        ])
        return _h.sha256(payload.encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def cache_size() -> int:
        """Сколько записей кэша извлечения лежит в Postgres (метрика/очистка)."""
        try:
            from src.api.services.config_store import config_store
            data = config_store.get_all("entity_cache") or {}
            return sum(1 for k in data if str(k).startswith("llm_"))
        except Exception:
            return 0

    @classmethod
    def get_active_preset(cls) -> str:
        """Получить имя активного пресета."""
        return cls._active_preset

    @classmethod
    def get_presets(cls) -> List[Dict]:
        """Получить список всех доступных пресетов."""
        return [
            {
                "id": pid,
                "name": p["name"],
                "description": p["description"],
                "use_cases": p["use_cases"],
                "entity_count": len(p["schema"].get("core", {})) + len(p["schema"].get("extended", {})),
                "relation_count": len(p["schema"].get("relations", {})),
                "is_active": pid == cls._active_preset
            }
            for pid, p in cls.SCHEMA_PRESETS.items()
        ]

    # ============================================================
    # Основной метод: двухпроходное извлечение
    # ============================================================

    async def extract_from_chunk(
        self,
        chunk_text: str,
        chunk_id: str,
        document_id: str,
        filename: str = ""
    ) -> Dict[str, Any]:
        """Извлечь сущности и связи из чанка ДВУМЯ последовательными LLM-вызовами.

        KGGen-подход (arxiv 2502.09956): сначала сущности, потом связи ТОЛЬКО
        между найденными сущностями. Это даёт консистентность: связи не ссылаются
        на несуществующие сущности (частая проблема одного объединённого промпта,
        где модель «выдумывает» source/target).

        Returns:
            {"entities": [...], "relations": [...], "facts": [...], "warnings": [...]}
        """
        if not chunk_text or len(chunk_text.strip()) < 20:
            return {"entities": [], "relations": [], "facts": [], "warnings": ["Chunk too short"]}

        cfg = self._get_graph_config()
        if not cfg or not cfg.get("model"):
            logger.warning(f"[graph] Функция 'graph' не настроена в админке — извлечение пропущено для {chunk_id}")
            return {"entities": [], "relations": [], "facts": [], "warnings": ["graph model not configured"]}

        # ── Кэш LLM-ответов: одинаковый текст чанка → тот же результат ──
        # Экономит токены при переиндексации и на повторяющихся фрагментах
        # (одинаковые страницы/таблицы в разных версиях документов).
        import hashlib as _hl
        _text_hash = _hl.sha256(chunk_text.encode('utf-8')).hexdigest()[:20]
        _ver = self.cache_version(cfg)
        _cache_key = f"llm_{_ver}_{_text_hash}"
        _legacy_key = f"llm_{_text_hash}"
        try:
            from src.api.services.config_store import config_store
            _cached = config_store.get("entity_cache", _cache_key)
            if not (_cached and isinstance(_cached, dict) and _cached.get("entities")):
                # Переходный путь: записи старого формата (без версии) переиспользуем и
                # переносим под новый ключ. Без этого смена формата ключа заставила бы
                # заново извлекать весь корпус (~3800 чанков × 2 LLM-вызова).
                _legacy = config_store.get("entity_cache", _legacy_key)
                if _legacy and isinstance(_legacy, dict) and _legacy.get("entities"):
                    _cached = _legacy
                    try:
                        config_store.set("entity_cache", _cache_key, _legacy)
                    except Exception:
                        pass
            if _cached and isinstance(_cached, dict) and _cached.get("entities"):
                logger.debug(f"[graph] Кэш LLM: {chunk_id} ({_cache_key[:16]}…)")
                return {
                    "entities": _cached.get("entities", []),
                    "relations": _cached.get("relations", []),
                    "facts": [],
                    "warnings": ["cache"],
                }
        except Exception:
            pass

        model = cfg.get("model")
        llm_url = cfg.get("url", "")
        api_key = cfg.get("api_key", "")
        provider = cfg.get("provider", "ollama")

        # Температура из параметров привязки функции graph (админка).
        # Ограничение сверху 0.5: извлечение графа требует стабильности,
        # высокая температура → галлюцинации/битый JSON.
        _params = cfg.get("parameters") or {}
        try:
            _temp = float(_params.get("temperature", 0.05))
        except (TypeError, ValueError):
            _temp = 0.05
        _temp = min(max(_temp, 0.0), 0.5)

        # Все типы сущностей (core + extended) в одном списке
        core_types = self._domain_config.get("core", {})
        extended_types = self._domain_config.get("extended", {})
        all_types = {**core_types, **extended_types}
        type_desc = "\n".join([f"  - {t}: {d}" for t, d in all_types.items()])

        rel_types = self._domain_config.get("relations", {})
        rel_desc = "\n".join([f"  - {t}: {d}" for t, d in rel_types.items()])

        sample = chunk_text[:1000]  # чанк максимум ~575 символов (500+overlap), 1000 с запасом

        # ── РЕЖИМ ИЗВЛЕЧЕНИЯ ─────────────────────────────────────────────
        # two_pass (по умолчанию): entities → relations (консистентность,
        # связи только между найденными сущностями, KGGen).
        # single_pass: ОДИН LLM-вызов (entities + relations в одном JSON) —
        # вдвое меньше запросов, чуть менее консистентные связи.
        extraction_mode = cfg.get("extraction_mode", "two_pass")

        if extraction_mode == "single":
            prompt_single = f"""Извлеки сущности и связи. Только JSON.

Типы сущностей:
{type_desc}

Типы связей:
{rel_desc}

Текст:
---
{sample}
---

JSON:
{{"entities":[{{"name":"...","type":"тип","confidence":0.0-1.0,"description":"1-2 слова чем является"}}],"relations":[{{"source":"...","target":"...","type":"тип связи"}}]}}

Правила:
- name строго из текста; type только из списка
- description: кратко чем является сущность (для дедупликации)
- source/target связей — ТОЛЬКО из списка entities этого же ответа, точные имена
- ничего не найдено → {{"entities":[],"relations":[]}}"""

            single_result = await self._call_llm(prompt_single, model, llm_url, chunk_id, "extract_all", api_key, provider, temperature=_temp)
            entities = single_result.get("entities", [])
            relations = single_result.get("relations", [])
        else:
            # ── ЭТАП 1: сущности ──────────────────────────────────────────
            # Сначала только entities (с description — нужно для entity resolution
            # и community detection). Связи — на этапе 2, между найденными сущностями.
            prompt_entities = f"""Извлеки сущности. Только JSON.

Типы сущностей:
{type_desc}

Текст:
---
{sample}
---

JSON:
{{"entities":[{{"name":"...","type":"тип","confidence":0.0-1.0,"description":"1-2 слова чем является"}}]}}

Правила:
- name строго из текста; type только из списка
- description: кратко чем является сущность (для дедупликации)
- ничего не найдено → {{"entities":[]}}"""

            entities_result = await self._call_llm(prompt_entities, model, llm_url, chunk_id, "extract_entities", api_key, provider, temperature=_temp)
            entities = entities_result.get("entities", [])

            # ── ЭТАП 2: связи между найденными сущностями ─────────────────
            relations = []
            if entities:
                # Список имён для консистентности: связи строим ТОЛЬКО между ними
                names = "\n".join([f"  - {e.get('name', '')}" for e in entities if e.get('name')])
                prompt_relations = f"""Извлеки связи между сущностями. Только JSON.

Типы связей:
{rel_desc}

Известные сущности (source/target ТОЛЬКО из них, точные имена):
{names}

Текст:
---
{sample}
---

JSON:
{{"relations":[{{"source":"...","target":"...","type":"тип связи"}}]}}

Правила:
- source и target: точные имена из списка известных сущностей
- связи нет → {{"relations":[]}}"""

                relations_result = await self._call_llm(prompt_relations, model, llm_url, chunk_id, "extract_relations", api_key, provider, temperature=_temp)
                relations = relations_result.get("relations", [])

        result = {"entities": entities, "relations": relations, "facts": [], "warnings": []}

        # Валидация
        warnings = self._validate_extraction(entities, relations)
        result["warnings"] = warnings

        # Сохраняем в кэш LLM (только если что-то извлечено)
        if entities:
            try:
                from src.api.services.config_store import config_store
                config_store.set("entity_cache", _cache_key, {
                    "entities": entities,
                    "relations": relations,
                })
            except Exception:
                pass

        return result

    # ============================================================
    # LLM вызов
    # ============================================================

    async def _call_llm(
        self, prompt: str, model: str, llm_url: str,
        chunk_id: str = "", pass_name: str = "",
        api_key: str = "", provider: str = "ollama",
        system_prompt: str = "", temperature: float = None
    ) -> Dict[str, Any]:
        """Вызов LLM с резервом: не получилось у основного — пробуем резервный провайдер.

        Резерв задаётся в привязке функции graph (Админка → Модели LLM): у одного провайдера
        могут быть одновременно чат, обработка документов и тесты на один ключ, и при сбое
        или лимитах шаг извлечения графа раньше просто падал с пустым результатом.
        """
        result = await self._call_llm_once(
            prompt=prompt, model=model, llm_url=llm_url, chunk_id=chunk_id, pass_name=pass_name,
            api_key=api_key, provider=provider, system_prompt=system_prompt, temperature=temperature)
        if self._has_graph_content(result):
            return result
        reserve = self._graph_reserve(model)
        if not reserve:
            return result
        _warn = ((result.get("warnings") or [""])[0] or "")[:80]
        logger.warning(f"[graph] {chunk_id} ({pass_name}): основной {provider}/{model} не дал "
                       f"результата ({_warn}) — пробую РЕЗЕРВ {reserve.get('provider')}/{reserve.get('model')}")
        result2 = await self._call_llm_once(
            prompt=prompt, model=reserve.get("model", ""), llm_url=reserve.get("url", ""),
            chunk_id=chunk_id, pass_name=f"{pass_name}:reserve", api_key=reserve.get("api_key", ""),
            provider=reserve.get("provider", "custom"),
            system_prompt=system_prompt or reserve.get("system_prompt", ""), temperature=temperature)
        if self._has_graph_content(result2):
            logger.warning(f"[graph] {chunk_id}: ответ дан РЕЗЕРВНЫМ провайдером "
                           f"{reserve.get('provider')}/{reserve.get('model')}")
            return result2
        return result

    @staticmethod
    def _has_graph_content(result: Dict[str, Any]) -> bool:
        """Есть ли в ответе хоть что-то полезное (сущности/связи/факты)."""
        return bool(result) and bool(result.get("entities") or result.get("relations") or result.get("facts"))

    def _graph_reserve(self, current_model: str) -> Optional[Dict[str, Any]]:
        """Резервный LLM-конфиг для функции graph (или None, если резерва нет)."""
        try:
            from src.api.services.provider_service import provider_service
            chain = provider_service.get_function_llm_chain("graph")
        except Exception:
            return None
        for cfg in chain[1:]:
            if cfg.get("model") and cfg.get("model") != current_model:
                return cfg
        return None

    async def _call_llm_once(
        self, prompt: str, model: str, llm_url: str,
        chunk_id: str = "", pass_name: str = "",
        api_key: str = "", provider: str = "ollama",
        system_prompt: str = "", temperature: float = None
    ) -> Dict[str, Any]:
        """Вызвать LLM и распарсить JSON-ответ.
        
        Поддерживает провайдеров:
        - ollama (по умолчанию): POST /api/generate
        - openai / deepseek / openrouter: POST /v1/chat/completions
        
        system_prompt берётся из настроек функции (function_map) — если не
        передан, используется дефолтный для извлечения сущностей.
        """
        if not system_prompt:
            # Брать system_prompt из настроек функции graph (извлечение сущностей),
            # если он задан. Восстановлен из backup — подробные правила типов/связей.
            try:
                _cfg = self._get_graph_config()
                if _cfg and _cfg.get("system_prompt"):
                    system_prompt = _cfg["system_prompt"]
            except Exception:
                pass
        if not system_prompt:
            system_prompt = "Ты — эксперт по извлечению структурированных данных из текста. Отвечай строго в JSON формате, без markdown-обёртки."

        # Страховка: не отправлять раздутый system_prompt (старые версии graph.txt
        # ~30 КБ могли остаться в config_store) — обрезаем до компактного дефолта.
        if len(system_prompt) > 2000:
            system_prompt = "Ты — эксперт по извлечению структурированных данных из текста. Отвечай строго в JSON формате, без markdown-обёртки."
        try:
            import aiohttp
            
            # «custom» — OpenAI-совместимые провайдеры, добавленные вручную (polza.ai и подобные):
            # тот же /v1/chat/completions. Без этого граф уходил в ветку Ollama и падал.
            if provider in ("openai", "deepseek", "openrouter", "gigachat", "custom"):
                # OpenAI-совместимый API (chat/completions)
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    # Температура из параметров привязки (админка), дефолт 0.05.
                    "temperature": temperature if temperature is not None else 0.05,
                    "max_tokens": 800
                }
                # ВАЖНО (почему так сделано): deepseek-v4-flash — reasoning-модель.
                # Она сначала генерирует длинный reasoning_content (размышления),
                # и только потом content. max_tokens ограничивает СУММАРНУЮ
                # генерацию — модель «думала» так долго, что упиралась в лимит
                # и возвращала ПУСТОЙ content (граф знаний не наполнялся,
                # ~70 сек на документ тратились впустую).
                # Параметр thinking:{"type":"disabled"} отключает размышления —
                # модель сразу пишет ответ. Проверено: content_len>0, reasoning=0.
                # max_tokens=800: ответ с сущностями обычно 500-1500 символов;
                # пробовали 400 — JSON обрезался на середине (невалидный),
                # поэтому 800 с запасом.
                # Отключаем размышления (thinking) для reasoning-моделей
                # (deepseek/openai/openrouter). Настройка no_think — в параметрах
                # привязки функции graph (админка); по умолчанию True.
                _no_think = True
                try:
                    _cfgx = self._get_graph_config() or {}
                    _no_think = bool((_cfgx.get("parameters") or {}).get("no_think", True))
                except Exception:
                    pass
                if provider in ("deepseek", "openai", "openrouter", "custom") and _no_think:
                    payload["thinking"] = {"type": "disabled"}
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{llm_url}/v1/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=180)
                    ) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            warning = f"LLM {provider} вернул {resp.status}: {text[:200]}"
                            logger.warning(f"Ошибка LLM для {chunk_id}: {warning}")
                            return {"entities": [], "relations": [], "facts": [], "warnings": [warning]}
                        data = await resp.json()
                        response = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            else:
                # Ollama API
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{llm_url}/api/generate",
                        json={
                            "model": model,
                            "prompt": prompt,
                            "stream": False,
                            "options": {"temperature": 0.05, "max_tokens": 400}
                        },
                        timeout=aiohttp.ClientTimeout(total=180)
                    ) as resp:
                        if resp.status != 200:
                            warning = f"LLM вернул {resp.status} (pass={pass_name})"
                            logger.warning(f"Ошибка LLM для {chunk_id}: {warning}")
                            return {"entities": [], "relations": [], "facts": [], "warnings": [warning]}
                        data = await resp.json()
                        response = data.get("response", "")
            
            result = self._parse_response(response)
            if not result.get("entities") and not result.get("relations") and not result.get("facts"):
                logger.debug(f"Пустой ответ LLM для {chunk_id} (pass={pass_name}): {response[:120]}")
            # Сохраняем сырой ответ — нужен type_watchdog (типизация),
            # который парсит JSON-список [{id,type}] напрямую из raw.
            result["raw"] = response
            return result

        except Exception as e:
            warning = f"{type(e).__name__}: {e}"
            logger.warning(f"Ошибка извлечения ({pass_name}) из {chunk_id}: {warning}")
            return {"entities": [], "relations": [], "facts": [], "warnings": [warning]}

    # ============================================================
    # Парсинг ответа LLM
    # ============================================================

    def _parse_response(self, response: str) -> Dict[str, Any]:
        """Распарсить JSON из ответа LLM.
        
        Устойчив к markdown-обёртке, лишним символам.
        """
        import json as _json
        
        text = response.strip()
        # Убираем markdown-код
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3].strip()

        try:
            data = _json.loads(text)
        except (_json.JSONDecodeError, ValueError):
            # Ищем JSON в тексте
            match = re.search(r'\{[^{}]*\}', text)
            if match:
                try:
                    data = _json.loads(match.group())
                except (_json.JSONDecodeError, ValueError):
                    return {}
            else:
                return {}

        result = {}
        if "entities" in data:
            result["entities"] = [
                {
                    "name": str(e.get("name", ""))[:200],
                    "type": str(e.get("type", "unknown")),
                    "confidence": min(1.0, max(0.0, float(e.get("confidence", 0.7)))),
                    "description": str(e.get("description", ""))[:300],
                }
                for e in data["entities"]
                if e.get("name") and len(str(e["name"]).strip()) > 1
            ]
        if "relations" in data:
            result["relations"] = [
                {
                    "source": str(r.get("source", "")),
                    "target": str(r.get("target", "")),
                    "type": str(r.get("type", "RELATED_TO"))
                }
                for r in data["relations"]
                if r.get("source") and r.get("target")
            ]
        if "facts" in data:
            result["facts"] = [str(f)[:300] for f in data["facts"] if f]

        return result

    # ============================================================
    # Валидация
    # ============================================================

    def _validate_extraction(
        self, entities: List[Dict], relations: List[Dict]
    ) -> List[str]:
        """Проверить качество извлечённых данных перед сохранением.
        
        Neo4j Best Practice: валидация на этапе extraction,
        а не post-hoc исправление ошибок в графе.
        """
        warnings = []
        valid_types = set()
        for group in self._domain_config.values():
            valid_types.update(group.keys())

        for e in entities:
            name = e.get("name", "")
            etype = e.get("type", "")
            # Слишком короткое имя — вероятно, мусор
            if len(name.strip()) < 2:
                warnings.append(f"Слишком короткое имя сущности: '{name}'")
            # Неизвестный тип
            if valid_types and etype not in valid_types and etype != "unknown":
                warnings.append(f"Неизвестный тип сущности: '{etype}' для '{name}'")

        return warnings

    # ============================================================
    # Сохранение в граф
    # ============================================================

    async def extract_and_store(
        self, document_id: str, chunk_id: str, chunk_text: str,
        chunk_seq: int = 0, filename: str = ""
    ):
        """Извлечь сущности из чанка и сохранить в Knowledge Graph.
        
        Полный пайплайн:
        1. Извлечение (итеративное, pass 1-3)
        2. Валидация
        3. Сохранение в Neo4j (Domain Graph)
        4. Сохранение в config_store (для UI)
        """
        try:
            from src.indexing.knowledge_graph import kg_service, Entity, Relation

            result = await self.extract_from_chunk(chunk_text, chunk_id, document_id, filename)

            entities = result.get("entities", [])
            relations = result.get("relations", [])

            # ── Самообозначения документа отбрасываем ───────────────────
            # Обозначение документа стоит в колонтитуле каждой страницы → попадает в
            # КАЖДЫЙ чанк → получает максимум упоминаний и становится крупнейшим узлом
            # графа, вытесняя смысловые сущности (замер 2026-09-13: 81% узлов графа —
            # справочные типы, самые частые обозначения — «34.10—2012», «бфбо-1.9-2024»).
            # Ссылки на ДРУГИЕ документы не трогаются: сравнение по точным вариантам
            # имени файла и по «свой номер + год», а не по произвольной подстроке.
            try:
                from src.indexing.entity_selfref import filter_self_references
                entities, relations, _dropped = filter_self_references(
                    entities, relations, filename
                )
                if _dropped:
                    logger.debug(
                        f"[graph] {document_id[:8]}/{chunk_id}: отброшены самообозначения: {_dropped[:5]}"
                    )
            except Exception as e:
                logger.warning(f"[graph] фильтр самообозначений не применён: {e}")

            if not entities:
                return

            # ── Страховка от зависания Neo4j ─────────────────────────────
            # create_entity/create_relation — СИНХРОННЫЕ вызовы драйвера neo4j.
            # Если Neo4j недоступен/завис, они блокируют event loop worker'а.
            # Выполняем их в отдельном потоке с таймаутом — зависший вызов
            # не заморозит обработку документа (аналогично _build_knowledge_graph_async).
            async def _neo4j_write(fn, *args, label: str = ""):
                # Таймаут из админки (Настройки Neo4j), дефолт 20с
                _t = NEO4J_TIMEOUT
                try:
                    from src.api.services.config_store import config_store
                    _cfg = config_store.get("neo4j", "config") or {}
                    _t = int(_cfg.get("timeout", NEO4J_TIMEOUT) or NEO4J_TIMEOUT)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(fn, *args),
                        timeout=_t,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[graph] Neo4j таймаут {label} ({_t}с) "
                        f"для {chunk_id} — пропуск"
                    )
                except Exception as e:
                    logger.warning(f"[graph] Neo4j ошибка {label} для {chunk_id}: {e}")

            # Сохраняем сущности в Domain Graph (БАТЧ: один UNWIND вместо N одиночных)
            entity_objs = []
            for e in entities:
                props = dict(e.get("properties", {}))
                # description из LLM — для entity resolution и community detection
                if e.get("description"):
                    props["description"] = e["description"]
                entity_objs.append(Entity(
                    name=e["name"],
                    type=e["type"],
                    chunk_id=chunk_id,
                    document_id=document_id,
                    confidence=e["confidence"],
                    properties=props
                ))
            if entity_objs:
                await _neo4j_write(kg_service.batch_create_entities, entity_objs, label="batch_entities")

            # Сохраняем связи (батч)
            rel_objs = [
                Relation(
                    source=r["source"], target=r["target"],
                    type=r["type"], document_id=document_id
                )
                for r in relations
            ]
            if rel_objs:
                await _neo4j_write(kg_service.batch_create_relations, rel_objs, label="batch_relations")

            # Сохраняем в config_store для быстрого доступа из UI
            from src.api.services.config_store import config_store
            key = f"entities_{document_id}"
            existing = config_store.get("entity_cache", key) or {"entities": [], "relations": []}
            existing["entities"].extend(entities)
            existing["relations"].extend(relations)
            config_store.set("entity_cache", key, existing)

            logger.debug(
                f"Извлечено из {chunk_id}: {len(entities)} сущностей, "
                f"{len(relations)} связей"
                + (f", предупреждений: {len(result.get('warnings',[]))}" if result.get("warnings") else "")
            )

        except Exception as e:
            logger.error(f"Ошибка extract_and_store для {chunk_id}: {e}")


# Глобальный экземпляр
entity_extractor = EntityExtractor()
