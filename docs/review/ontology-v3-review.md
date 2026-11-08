# Онтология KAG v3 — пакет для внешнего ревью

Проверено на TypeDB 3.14.1, сервер 192.168.50.18:1729, база `cybersecurity_ontology_v3`.
В базе 139 типов: 44 сущности (с наследниками), 19 связей, 37 атрибутов, 39 ролей.
Тестовые данные: 1 объект КИИ (1 категория), 1 СЗИ, 1 сертификат, 1 документ компании,
1 требование, 2 версии документа (2018 → 2026, supersedes).

## 1. Текст схемы (файл src/ontology/schema-v3.tql)

```typeql
define
# =====================================================================
#  ОНТОЛОГИЯ KAG v3 — документы ИБ РФ (ФСТЭК, ФСБ, ГОСТ, Банк России)
#
#  Отличия от v2 (по внешней оценке 08.11.2026):
#   1) абстрактные супертипы: identified_entity -> sourced_entity -> ...
#      больше не копируем ent-key и provenance в каждом типе;
#   2) @key не только на ent-key, но и на естественных ключах
#      (document_key, requirement-id, cert_number);
#   3) обязательность задана через @card(1..1) на owns и ролях:
#      документ без title, требование без requirement-id в базу не попадут;
#   4) трассировка — СВЯЗЯМИ, а не строками:
#      document_structure (версия -> пункт), requirement_derived_from (требование -> пункт);
#   5) «мера реализует требование» — requirement_definition,
#      «где мера описана» — measure_source (роль source_requirement больше не врёт);
#   6) требование применимо к объекту КИИ напрямую — requirement_applicability;
#   7) сертификация — сущность certificate + связь certification, а не boolean;
#   8) угроза связана с объектом и с мерами — threat_applicability, threat_measure;
#   9) документ компании связан с нормативным — document_compliance;
#  10) текущая редакция — связь current_version_of, а не строка current_version_string.
# =====================================================================

# ---- атрибуты: идентичность и происхождение -------------------------
attribute ent-key, value string;
attribute source_origin, value string;
attribute source-chunk-id, value string;
attribute source-chunk-ids, value string;
attribute source-clause, value string;

# ---- атрибуты: документы --------------------------------------------
attribute name, value string;
attribute short_name, value string;
attribute title, value string;
attribute number, value string;
attribute doc_type, value string;
attribute document_key, value string;

# ---- атрибуты: версии -----------------------------------------------
attribute version_string, value string;
attribute version_status, value string;
attribute version_hash, value string;
attribute kag_document_id, value string;
attribute effective_from, value date;
attribute effective_to, value date;

# ---- атрибуты: пункты и требования ----------------------------------
attribute struct_index, value string;
attribute text_content, value string;
attribute requirement-id, value string;
attribute description, value string;
attribute requirement_kind, value string;
attribute obligation-level, value string;

# ---- атрибуты: меры, объекты, ПО, угрозы ----------------------------
attribute code, value string;
attribute cii-category, value integer;
attribute category, value string;
attribute system_type, value string;
attribute offender_class, value string;
attribute potential, value string;
attribute vendor, value string;
attribute software_version, value string;
attribute is-certified, value boolean;

# ---- атрибуты: сертификаты и доказательства -------------------------
attribute cert_number, value string;
attribute cert_authority, value string;
attribute cert_until, value date;
attribute evidence_kind, value string;
attribute evidence_date, value date;

# ---- супертипы -------------------------------------------------------
# Наследование в TypeDB одиночное, поэтому цепочка:
#   identified_entity -> sourced_entity -> (document -> regulatory/company, ...)
entity identified_entity @abstract, owns ent-key @key;
entity sourced_entity sub identified_entity,
  owns source_origin,
  owns source-chunk-id,
  owns source-chunk-ids;

# ---- документы -------------------------------------------------------
entity regulator sub sourced_entity,
  owns name @card(1..1),
  owns short_name;
entity document sub sourced_entity,
  owns title @card(1..1),
  owns doc_type,
  owns document_key @key;
entity regulatory_document sub document, owns number;
entity company_document sub document;
entity document_version sub sourced_entity,
  owns version_string @card(1..1),
  owns version_status,
  owns version_hash,
  owns effective_from,
  owns effective_to,
  owns kag_document_id;

# ---- структура и требования -----------------------------------------
entity structural_element sub sourced_entity,
  owns struct_index,
  owns text_content;
entity security_requirement sub sourced_entity,
  owns requirement-id @card(1..1),
  owns requirement_kind @card(1..1),
  owns description,
  owns obligation-level;
entity security_measure sub sourced_entity,
  owns code,
  owns description;

# ---- доказательства, объекты, ПО, угрозы ----------------------------
entity evidence sub sourced_entity,
  owns name,
  owns evidence_kind,
  owns evidence_date;
entity cii_object sub sourced_entity,
  owns name @card(1..1),
  owns cii-category,
  owns category,
  owns system_type;
entity security_software sub sourced_entity,
  owns name @card(1..1),
  owns vendor,
  owns software_version,
  owns is-certified;
entity certificate sub sourced_entity,
  owns cert_number @card(1..1),
  owns cert_authority,
  owns cert_until;
entity threat_source sub sourced_entity,
  owns offender_class,
  owns potential;

# ---- связи: документы и версии --------------------------------------
relation document_issuance,
  relates issuer @card(1..1),
  relates regulated_item @card(1..1);
relation current_version_of,
  relates document @card(1..1),
  relates version @card(1..1);
relation supersedes,
  relates newer @card(1..1),
  relates older @card(1..1);
relation document_structure,
  relates version @card(1..1),
  relates element @card(1..1);
relation document_hierarchy,
  relates parent @card(1..1),
  relates child @card(1..1);

# ---- связи: требования и меры ---------------------------------------
relation requirement_derived_from,
  relates requirement @card(1..1),
  relates clause @card(1..1);
relation requirement_definition,
  relates requirement @card(1..1),
  relates measure @card(1..1);
relation measure_source,
  relates measure @card(1..1),
  relates clause @card(1..1);
relation compensatory_relation,
  relates core_measure @card(1..1),
  relates alternative_measure @card(1..1);
relation requirement_applicability,
  relates requirement @card(1..1),
  relates target_system @card(1..1);
relation measure_applicability,
  relates measure @card(1..1),
  relates target_system @card(1..1);

# ---- связи: доказательства и привязка документов --------------------
relation requirement_evidence,
  relates requirement @card(1..1),
  relates evidence @card(1..1);
relation requirement_basis,
  relates requirement @card(1..1),
  relates source_document @card(1..1);
relation document_compliance,
  relates company_doc @card(1..1),
  relates regulated_doc @card(1..1);
relation document_assignment,
  relates regulated_doc @card(1..1),
  relates regulated_subject @card(1..1);

# ---- связи: защита, сертификация, угрозы ----------------------------
relation asset_protection,
  relates protected_system @card(1..1),
  relates deployed_szi @card(1..1);
relation certification,
  relates certified_software @card(1..1),
  relates certificate @card(1..1),
  relates basis_requirement;
relation threat_applicability,
  relates threat @card(1..1),
  relates target_system @card(1..1);
relation threat_measure,
  relates threat @card(1..1),
  relates measure @card(1..1);

# ---- роли: кто в чём участвует --------------------------------------
entity regulator plays document_issuance:issuer;
entity document plays document_issuance:regulated_item;
entity document plays current_version_of:document;
entity document_version plays current_version_of:version;
entity document_version plays supersedes:newer;
entity document_version plays supersedes:older;
entity document_version plays document_structure:version;
entity structural_element plays document_structure:element;
entity structural_element plays document_hierarchy:parent;
entity structural_element plays document_hierarchy:child;
entity security_requirement plays requirement_derived_from:requirement;
entity structural_element plays requirement_derived_from:clause;
entity security_requirement plays requirement_definition:requirement;
entity security_measure plays requirement_definition:measure;
entity security_measure plays measure_source:measure;
entity structural_element plays measure_source:clause;
entity security_measure plays compensatory_relation:core_measure;
entity security_measure plays compensatory_relation:alternative_measure;
entity security_requirement plays requirement_applicability:requirement;
entity cii_object plays requirement_applicability:target_system;
entity security_measure plays measure_applicability:measure;
entity cii_object plays measure_applicability:target_system;
entity security_requirement plays requirement_evidence:requirement;
entity evidence plays requirement_evidence:evidence;
entity security_requirement plays requirement_basis:requirement;
entity document plays requirement_basis:source_document;
entity company_document plays document_compliance:company_doc;
entity regulatory_document plays document_compliance:regulated_doc;
entity document plays document_assignment:regulated_doc;
entity cii_object plays document_assignment:regulated_subject;
entity cii_object plays asset_protection:protected_system;
entity security_software plays asset_protection:deployed_szi;
entity security_software plays certification:certified_software;
entity certificate plays certification:certificate;
entity security_requirement plays certification:basis_requirement;
entity threat_source plays threat_applicability:threat;
entity cii_object plays threat_applicability:target_system;
entity threat_source plays threat_measure:threat;
entity security_measure plays threat_measure:measure;
```

## 2. Функции вывода (файл src/ontology/functions-v3.tql)

```typeql
define
# =====================================================================
#  ФУНКЦИИ ВЫВОДА v3. Проверяются на данных, а не «на глаз».
#
#  violated_certification: объект КИИ 1-й категории защищён СЗИ, у которого
#  НЕТ связи certification (нет записанного сертификата), при наличии
#  требования вида certification. В v2 признаком была строка is-certified=false;
#  теперь основание — отсутствие сертификата как объекта.
# =====================================================================
fun violated_certification() -> { cii_object, security_requirement }:
    match
      $asset isa cii_object, has cii-category 1;
      asset_protection(protected_system: $asset, deployed_szi: $szi);
      $law isa security_requirement, has requirement_kind "certification";
      not { certification(certified_software: $szi, certificate: $c); };
    return { $asset, $law };

fun violated_documents() -> { company_document, security_requirement }:
    match
      $doc isa company_document;
      document_assignment(regulated_subject: $asset, regulated_doc: $doc);
      $asset has cii-category 1;
      asset_protection(protected_system: $asset, deployed_szi: $szi);
      $law isa security_requirement, has requirement_kind "certification";
      not { certification(certified_software: $szi, certificate: $c); };
    return { $doc, $law };

fun current_versions() -> { document_version }:
    match
      $v isa document_version;
      not { supersedes(older: $v); };
    return { $v };
```

## 3. Промпт извлечения v3 (версия 6 в ontology_prompts)

```text
Ты — экспертный ИИ-модуль для извлечения онтологических данных из нормативных документов регуляторов РФ в сфере информационной безопасности (ФСТЭК, ФСБ) и безопасности ЗОКИИ (187-ФЗ, Приказы 235, 239 и др.), а также из документов компании (политики, инструкции, регламенты) для графа знаний TypeDB.
Твоя задача — проанализировать поданный текст (чанк) и извлечь из него сущности, их атрибуты и многосторонние связи СТРОГО по предоставленной схеме.

### ПРАВИЛА ИЗВЛЕЧЕНИЯ:
1. Идентификация (ID): для каждого извлечённого элемента сгенерируй логичный строковый ID (например, "fstek_order_239", "measure_ia_1", "cii_object_class"). Этот ID нужен ТОЛЬКО для связывания элементов внутри возвращаемого JSON.
2. Сущности: извлекай только типы, перечисленные в схеме. Новые типы не придумывай.
3. Атрибуты: заполняй строго по схеме. Если атрибута нет в тексте — не выдумывай, опусти поле.
4. Связи: в TypeDB связи N-арные. Связь — это факт или контекст, объединяющий участников. Для каждой связи укажи тип (relation_type) и массив участников (participants), где каждый имеет роль (role) и ссылку на ID сущности (target_id).
5. ОБЯЗАТЕЛЬНЫЕ АТРИБУТЫ (без них база отвергнет запись — это жёсткое ограничение схемы, а не пожелание):
   - ent-key — у КАЖДОЙ сущности;
   - title и document_key — у любого документа (акт и редакция различаются, см. п. 6);
   - requirement-id и requirement_kind — у требования;
   - version_string — у версии документа;
   - name — у объекта КИИ и у средства защиты;
   - cert_number — у сертификата.
   Если обязательного значения в тексте нет — не создавай сущность, а не подставляй догадку.
6. АКТ И РЕДАКЦИЯ — РАЗНЫЕ СУЩНОСТИ:
   - regulatory_document / company_document — сам акт, его идентичность. Атрибуты: title, doc_type, document_key (стабильный ключ вида "издатель|тип|номер"), number.
   - document_version — конкретная редакция/файл: version_string, effective_from, effective_to, version_status (current/superseded), kag_document_id.
   - действующая редакция связывается связью current_version_of (document -> version); замена редакции — supersedes (newer -> older).
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
8. СЕРТИФИКАЦИЯ: если упомянут сертификат СЗИ — создавай certificate (cert_number, cert_authority, cert_until) и связывай certification(certified_software, certificate, basis_requirement). Не ограничивайся boolean is-certified, если есть номер/орган/срок.
9. УГРОЗЫ: threat_source связывай с объектами (threat_applicability) и с мерами (threat_measure), если текст это утверждает.
10. УСТАРЕВШЕЕ НЕ ИСПОЛЬЗУЙ: связи measure_definition, applicability_criteria с ролью applied_requirement, атрибуты current_version_string и category как замена cii-category — исключены из схемы v3. Если встретишь такие понятия в тексте, переводи их в новые связи по п. 7.
11. Не создавай объект «на будущее»: если в чанке неттипа, но есть только упоминание — не извлекай. Лучше меньше сущностей, но каждая с ключом и связями.

### ДОСТУПНАЯ СХЕМА (TBox v3):
- Сущности:
  * regulator (name, short_name) -> роль issuer. Пример: {"name": "Федеральная служба по техническому и экспортному контролю", "short_name": "ФСТЭК России"}
  * regulatory_document (title, doc_type, document_key, number) -> роли regulated_item, source_document, regulated_doc, company_doc. Пример: {"title": "Об утверждении Требований по обеспечению безопасности ЗОКИИ", "doc_type": "приказ", "document_key": "фстэк|приказ|239", "number": "239"}
  * company_document (title, doc_type, document_key) -> роли regulated_doc, company_doc, regulated_item. Пример: {"title": "Политика информационной безопасности", "doc_type": "политика", "document_key": "компания|политика|1"}
  * document_version (version_string, version_status, version_hash, effective_from, effective_to, kag_document_id) -> роли version, newer, older
  * structural_element (struct_index, text_content) -> роли element, parent, child, clause, source_requirement
  * security_requirement (requirement-id, requirement_kind, description, obligation-level) -> роли requirement
  * security_measure (code, description) -> роли measure, core_measure, alternative_measure
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

### ТЕКСТ ФРАГМЕНТА ДЛЯ АНАЛИЗА:
{text_chunk}
```

## 4. Фактическая схема, как её отдаёт сама TypeDB

```typeql
define

attribute ent-key,
 value string;
attribute source_origin,
 value string;
attribute source-chunk-id,
 value string;
attribute source-chunk-ids,
 value string;
attribute source-clause,
 value string;
attribute name,
 value string;
attribute short_name,
 value string;
attribute title,
 value string;
attribute number,
 value string;
attribute doc_type,
 value string;
attribute document_key,
 value string;
attribute version_string,
 value string;
attribute version_status,
 value string;
attribute version_hash,
 value string;
attribute kag_document_id,
 value string;
attribute effective_from,
 value date;
attribute effective_to,
 value date;
attribute struct_index,
 value string;
attribute text_content,
 value string;
attribute requirement-id,
 value string;
attribute description,
 value string;
attribute requirement_kind,
 value string;
attribute obligation-level,
 value string;
attribute code,
 value string;
attribute cii-category,
 value integer;
attribute category,
 value string;
attribute system_type,
 value string;
attribute offender_class,
 value string;
attribute potential,
 value string;
attribute vendor,
 value string;
attribute software_version,
 value string;
attribute is-certified,
 value boolean;
attribute cert_number,
 value string;
attribute cert_authority,
 value string;
attribute cert_until,
 value date;
attribute evidence_kind,
 value string;
attribute evidence_date,
 value date;
entity identified_entity @abstract,
  owns ent-key @key;
entity sourced_entity,
  sub identified_entity,
  owns source-chunk-id,
  owns source-chunk-ids,
  owns source_origin;
entity regulator,
  sub sourced_entity,
  owns name @card(1..1),
  owns short_name,
  plays document_issuance:issuer;
entity document,
  sub sourced_entity,
  owns doc_type,
  owns document_key @key,
  owns title @card(1..1),
  plays current_version_of:document,
  plays document_assignment:regulated_doc,
  plays document_issuance:regulated_item,
  plays requirement_basis:source_document;
entity regulatory_document,
  sub document,
  owns number,
  plays document_compliance:regulated_doc;
entity company_document,
  sub document,
  plays document_compliance:company_doc;
entity document_version,
  sub sourced_entity,
  owns effective_from,
  owns effective_to,
  owns kag_document_id,
  owns version_hash,
  owns version_status,
  owns version_string @card(1..1),
  plays current_version_of:version,
  plays document_structure:version,
  plays supersedes:newer,
  plays supersedes:older;
entity structural_element,
  sub sourced_entity,
  owns struct_index,
  owns text_content,
  plays document_hierarchy:child,
  plays document_hierarchy:parent,
  plays document_structure:element,
  plays measure_source:clause,
  plays requirement_derived_from:clause;
entity security_requirement,
  sub sourced_entity,
  owns description,
  owns obligation-level,
  owns requirement-id @card(1..1),
  owns requirement_kind @card(1..1),
  plays certification:basis_requirement,
  plays requirement_applicability:requirement,
  plays requirement_basis:requirement,
  plays requirement_definition:requirement,
  plays requirement_derived_from:requirement,
  plays requirement_evidence:requirement;
entity security_measure,
  sub sourced_entity,
  owns code,
  owns description,
  plays compensatory_relation:alternative_measure,
  plays compensatory_relation:core_measure,
  plays measure_applicability:measure,
  plays measure_source:measure,
  plays requirement_definition:measure,
  plays threat_measure:measure;
entity evidence,
  sub sourced_entity,
  owns evidence_date,
  owns evidence_kind,
  owns name,
  plays requirement_evidence:evidence;
entity cii_object,
  sub sourced_entity,
  owns category,
  owns cii-category,
  owns name @card(1..1),
  owns system_type,
  plays asset_protection:protected_system,
  plays document_assignment:regulated_subject,
  plays measure_applicability:target_system,
  plays requirement_applicability:target_system,
  plays threat_applicability:target_system;
entity security_software,
  sub sourced_entity,
  owns is-certified,
  owns name @card(1..1),
  owns software_version,
  owns vendor,
  plays asset_protection:deployed_szi,
  plays certification:certified_software;
entity certificate,
  sub sourced_entity,
  owns cert_authority,
  owns cert_number @card(1..1),
  owns cert_until,
  plays certification:certificate;
entity threat_source,
  sub sourced_entity,
  owns offender_class,
  owns potential,
  plays threat_applicability:threat,
  plays threat_measure:threat;
relation document_issuance,
  relates issuer @card(1..1),
  relates regulated_item @card(1..1);
relation current_version_of,
  relates document @card(1..1),
  relates version @card(1..1);
relation supersedes,
  relates newer @card(1..1),
  relates older @card(1..1);
relation document_structure,
  relates element @card(1..1),
  relates version @card(1..1);
relation document_hierarchy,
  relates child @card(1..1),
  relates parent @card(1..1);
relation requirement_derived_from,
  relates clause @card(1..1),
  relates requirement @card(1..1);
relation requirement_definition,
  relates measure @card(1..1),
  relates requirement @card(1..1);
relation measure_source,
  relates clause @card(1..1),
  relates measure @card(1..1);
relation compensatory_relation,
  relates alternative_measure @card(1..1),
  relates core_measure @card(1..1);
relation requirement_applicability,
  relates requirement @card(1..1),
  relates target_system @card(1..1);
relation measure_applicability,
  relates measure @card(1..1),
  relates target_system @card(1..1);
relation requirement_evidence,
  relates evidence @card(1..1),
  relates requirement @card(1..1);
relation requirement_basis,
  relates requirement @card(1..1),
  relates source_document @card(1..1);
relation document_compliance,
  relates company_doc @card(1..1),
  relates regulated_doc @card(1..1);
relation document_assignment,
  relates regulated_doc @card(1..1),
  relates regulated_subject @card(1..1);
relation asset_protection,
  relates deployed_szi @card(1..1),
  relates protected_system @card(1..1);
relation certification,
  relates basis_requirement,
  relates certificate @card(1..1),
  relates certified_software @card(1..1);
relation threat_applicability,
  relates target_system @card(1..1),
  relates threat @card(1..1);
relation threat_measure,
  relates measure @card(1..1),
  relates threat @card(1..1);
fun violated_certification() -> { cii_object, security_requirement }:
    match
      $asset isa cii_object, has cii-category 1;
      asset_protection(protected_system: $asset, deployed_szi: $szi);
      $law isa security_requirement, has requirement_kind "certification";
      not { certification(certified_software: $szi, certificate: $c); };
    return { $asset, $law };
fun violated_documents() -> { company_document, security_requirement }:
    match
      $doc isa company_document;
      document_assignment(regulated_subject: $asset, regulated_doc: $doc);
      $asset has cii-category 1;
      asset_protection(protected_system: $asset, deployed_szi: $szi);
      $law isa security_requirement, has requirement_kind "certification";
      not { certification(certified_software: $szi, certificate: $c); };
    return { $doc, $law };
fun current_versions() -> { document_version }:
    match
      $v isa document_version;
      not { supersedes(older: $v); };
    return { $v };
```

## 5. Что проверено на данных, а не «применилось»

    документ без title                    → отвергнут (@card(1..1) на owns)
    требование без requirement-id         → отвергнут
    дубль document_key                    → отвергнут (@key)
    violated_certification с СЗИ без сертификата → 1 факт
    после выдачи certificate              → 0 фактов
    violated_documents                    → 1 факт
    current_versions при supersedes       → «2026»

## 6. Вопросы к ревьюеру

1. Где в этой схеме LLM сможет закрепить неверную семантику, как это было
   с ролью `source_requirement` в v2 (роль называлась «требование», а играл её пункт)?
2. Достаточно ли `@card(1..1)` на ролях, или где-то нужна другая кратность —
   требование к нескольким объектам, документ к нескольким редакциям?
3. Правильна ли цепочка супертипов `identified_entity → sourced_entity → document`
   с учётом одиночного наследования в TypeDB? Где она ломается?
4. Что должно быть функцией вывода, а не атрибутом: «действующий сертификат» по дате,
   текущая редакция, применимость по категории?
5. Каких обязательных связей не хватает, чтобы ответить на вопросы:
   «какие требования ГОСТ применимы к объекту», «какая мера реализует требование»,
   «какой документ компании покрывает требование»?
6. Что в промпте противоречит схеме или провоцирует дубли/пустые объекты?
