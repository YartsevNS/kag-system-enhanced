"""SQLAlchemy model: версии схемы онтологии TypeDB (ADR-004, пункт «сохранение»).

Зачем таблица, а не файл: требование владельца — «изменения версионируются/журналируются
(кто, когда, что изменил), с откатом к предыдущей версии схемы». Файл в контейнере это
не даёт (переживает ли пересборку — нет; кто изменил — не видно; два админа затрут друг друга).

Что хранится:
- snapshot_json — СТРУКТУРА схемы на момент версии (типы, owns, plays, роли). По ней
  считается разница при откате; она не зависит от форматирования текста.
- define_text — текст TypeQL, которым это состояние получено (для чтения глазами и экспорта).
- action — что сделали: apply (правка текстом), create, delete, restore (откат), template (шаблон).
- ok/error — неудачная попытка тоже попадает в журнал: «пробовал применить и не вышло» —
  это событие, которое ищут при разборе «почему схема не такая».

Таблица создаётся существующим ensure_schema (create_all) — отдельная миграция не нужна.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text

from src.database.models import Base


class OntologySchemaVersion(Base):
    __tablename__ = "ontology_schema_versions"
    __table_args__ = (
        Index("ix_osv_created", "created_at"),
    )

    id = Column(String, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    author = Column(String, default="")            # имя пользователя, сделавшего изменение
    action = Column(String, default="apply")       # apply | create | delete | restore | template
    comment = Column(Text, default="")             # короткое пояснение «что и зачем»
    define_text = Column(Text, default="")         # TypeQL, которым получено состояние
    snapshot_json = Column(Text, default="{}")     # структура схемы (JSON)
    ntypes = Column(Integer, default=0)            # число типов для списка
    ok = Column(Boolean, default=True)             # попытка удалась
    error = Column(Text, default="")               # текст ошибки, если не удалась
    restored_from = Column(String, default="")     # id версии, к которой откатывались

    def to_dict(self, with_snapshot: bool = False) -> dict:
        data = {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "author": self.author or "",
            "action": self.action or "",
            "comment": self.comment or "",
            "ntypes": self.ntypes or 0,
            "ok": bool(self.ok),
            "error": self.error or "",
            "restored_from": self.restored_from or "",
            "define_len": len(self.define_text or ""),
        }
        if with_snapshot:
            data["define_text"] = self.define_text or ""
            data["snapshot_json"] = self.snapshot_json or "{}"
        return data
