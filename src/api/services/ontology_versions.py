"""Версии схемы онтологии и откат (ADR-004, пункт «сохранение»).

ЧТО ДЕЛАЕТ. Каждое изменение схемы (правка текстом, создание/удаление типа, применение
шаблона, откат) записывается версией: кто, когда, что, какой текст применён и КАКАЯ СТРУКТУРА
получилась. Откат считается по структуре, а не повторным проигрыванием текста: чужие
изменения за это время не «перезатрут» друг друга молча.

ЧЕСТНОЕ ОГРАНИЧЕНИЕ TypeDB 3.14 (проверено живьём, не по документации): `undefine` умеет
удалять ТОЛЬКО тип целиком. Снять отдельный `owns`/`plays`/`relates` нельзя — все варианты
синтаксиса дают [TSV11]. Поэтому откат умеет:
  - удалить типы, которых в целевой версии не было;
  - добавить недостающие типы, owns и plays;
и НЕ умеет снять лишние owns/plays у существующего типа — такие расхождения возвращаются
списком `unresolved`, и UI показывает их явно, а не делает вид, что откат полный.

ПОЧЕМУ ТАК, А НЕ «УДАЛИТЬ ТИП И СОЗДАТЬ ЗАНОВО»: удаление типа уничтожает экземпляры и связи
в данных, то есть откат схемы превратился бы в уничтожение доказательной базы. Лучше
неполный откат с явным списком, чем молчаливая потеря данных.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.api.services.ontology_service import OntologyUnavailable, ontology_service
from src.database.ontology_models import OntologySchemaVersion
from src.database.session import get_session_local

logger = logging.getLogger(__name__)


def snapshot_of(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Нормализованная структура схемы для хранения и сравнения (без служебных полей)."""
    return {
        "entities": [
            {"label": e["label"], "owns": sorted(e.get("owns") or []), "plays": sorted(e.get("plays") or [])}
            for e in sorted(schema.get("entities", []), key=lambda x: x["label"])
        ],
        "relations": [
            {"label": r["label"], "owns": sorted(r.get("owns") or []), "roles": sorted(r.get("roles") or [])}
            for r in sorted(schema.get("relations", []), key=lambda x: x["label"])
        ],
        "attributes": [
            {"label": a["label"], "value": a.get("value") or "string"}
            for a in sorted(schema.get("attributes", []), key=lambda x: x["label"])
        ],
        "ntypes": int(schema.get("ntypes") or 0),
    }


def _index(snap: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Словарь «тип -> запись» с категорией, чтобы сравнивать одним проходом."""
    out: Dict[str, Dict[str, Any]] = {}
    for e in snap.get("entities", []):
        out[e["label"]] = {"kind": "entity", **e}
    for r in snap.get("relations", []):
        out[r["label"]] = {"kind": "relation", **r}
    for a in snap.get("attributes", []):
        out[a["label"]] = {"kind": "attribute", **a}
    return out


def diff_snapshots(current: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
    """Что сделать, чтобы из current получилось target. Возвращает план без побочных эффектов."""
    cur, tgt = _index(current), _index(target)

    drop = []
    for label, item in cur.items():
        if label not in tgt:
            drop.append({"kind": item["kind"], "label": label})
    # Порядок удаления: сущности -> связи -> атрибуты. Связь удаляется вместе со своими ролями,
    # атрибут — последним: на него могут ссылаться owns (снять owns в 3.14 нельзя).
    order = {"entity": 0, "relation": 1, "attribute": 2}
    drop.sort(key=lambda x: (order.get(x["kind"], 3), x["label"]))

    add = [{"kind": v["kind"], "label": k, **v} for k, v in tgt.items() if k not in cur]
    add.sort(key=lambda x: ({"attribute": 0, "entity": 1, "relation": 2}.get(x["kind"], 3), x["label"]))

    add_owns, add_plays, unresolved = [], [], []
    for label, want in tgt.items():
        have = cur.get(label)
        if not have:
            continue  # тип добавляется целиком — его owns/plays уйдут в составе add
        for attr in want.get("owns") or []:
            if attr not in (have.get("owns") or []):
                add_owns.append({"type": label, "kind": want["kind"], "attr": attr})
        for role in want.get("plays") or []:
            if role not in (have.get("plays") or []):
                add_plays.append({"type": label, "role": role})
        # Снять лишнее TypeDB 3.14 не даёт — сообщаем, а не молчим
        for attr in (have.get("owns") or []):
            if attr not in (want.get("owns") or []):
                unresolved.append({"what": "owns", "type": label, "item": attr,
                                   "why": "в TypeDB 3.14 нельзя снять owns, не удалив тип"})
        for role in (have.get("plays") or []):
            if role not in (want.get("plays") or []):
                unresolved.append({"what": "plays", "type": label, "item": role,
                                   "why": "в TypeDB 3.14 нельзя снять plays, не удалив тип"})

    return {"drop": drop, "add": add, "add_owns": add_owns, "add_plays": add_plays,
            "unresolved": unresolved,
            "empty": not (drop or add or add_owns or add_plays)}


class OntologyVersionService:
    """Журнал версий и откат. Пишет в таблицу ontology_schema_versions."""

    # ---- журнал -------------------------------------------------------
    def record(
        self,
        author: str,
        action: str,
        define_text: str = "",
        comment: str = "",
        ok: bool = True,
        error: str = "",
        restored_from: str = "",
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Записать версию. snapshot=None — прочитать схему из TypeDB (после успешного изменения)."""
        if snapshot is None:
            try:
                snapshot = snapshot_of(ontology_service.get_schema())
            except OntologyUnavailable:
                snapshot = {"entities": [], "relations": [], "attributes": [], "ntypes": 0}
            except Exception as exc:  # noqa: BLE001 — журнал не должен валить операцию
                logger.warning("версия схемы: снапшот не прочитан: %s", str(exc).splitlines()[-1][:120])
                snapshot = {"entities": [], "relations": [], "attributes": [], "ntypes": 0}

        row = OntologySchemaVersion(
            id=uuid.uuid4().hex,
            created_at=datetime.now(timezone.utc),
            author=(author or "")[:120],
            action=action or "apply",
            comment=(comment or "")[:2000],
            define_text=define_text or "",
            snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            ntypes=int(snapshot.get("ntypes") or 0),
            ok=bool(ok),
            error=(error or "")[:2000],
            restored_from=(restored_from or "")[:64],
        )
        # Значения фиксируем ДО закрытия сессии: после commit объект отсоединён, и обращение
        # к row.id/row.ntypes поднимает «Instance is not bound to a Session» — снаружи это
        # выглядело как «версия не записалась», хотя запись уже была в таблице.
        row_id = row.id
        ntypes_val = int(snapshot.get("ntypes") or 0)
        ok_val = bool(ok)

        try:
            Session = get_session_local()
            db = Session()
            try:
                db.add(row)
                db.commit()
            finally:
                db.close()
            return {"id": row_id, "ntypes": ntypes_val, "ok": ok_val}
        except Exception as exc:  # noqa: BLE001 — версия не записалась, но схема уже изменена
            logger.error("версия схемы не записана: %s", str(exc).splitlines()[-1][:200])
            return {"id": "", "error": str(exc).splitlines()[-1][:200]}

    def list_versions(self, limit: int = 50) -> List[Dict[str, Any]]:
        Session = get_session_local()
        db = Session()
        try:
            rows = (db.query(OntologySchemaVersion)
                    .order_by(OntologySchemaVersion.created_at.desc())
                    .limit(max(1, min(int(limit or 50), 200)))
                    .all())
            out = []
            for r in rows:
                item = r.to_dict()
                # Полнота текста версии: у записей до правки экспорта аннотаций нет.
                # Флаг нужен, чтобы UI предупреждал: такой файл не отражает схему.
                text = r.define_text or ""
                item["has_annotations"] = ("@key" in text or "@card" in text
                                           or "@abstract" in text)
                out.append(item)
            return out
        finally:
            db.close()

    def get_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        Session = get_session_local()
        db = Session()
        try:
            row = db.get(OntologySchemaVersion, version_id)
            return row.to_dict(with_snapshot=True) if row else None
        finally:
            db.close()

    def _snapshot_of_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        Session = get_session_local()
        db = Session()
        try:
            row = db.get(OntologySchemaVersion, version_id)
            if not row or not row.ok:
                return None
            try:
                return json.loads(row.snapshot_json or "{}")
            except Exception:
                return None
        finally:
            db.close()

    # ---- откат --------------------------------------------------------
    def restore(self, version_id: str, author: str, comment: str = "") -> Dict[str, Any]:
        """Привести схему к структуре указанной версии. Возвращает отчёт по шагам."""
        target = self._snapshot_of_version(version_id)
        if target is None:
            return {"ok": False, "error": "версия не найдена или была неудачной", "steps": []}

        current = snapshot_of(ontology_service.get_schema())
        plan = diff_snapshots(current, target)

        if plan["empty"]:
            return {"ok": True, "steps": [], "unresolved": plan["unresolved"],
                    "message": "схема уже совпадает с выбранной версией"}

        steps: List[Dict[str, Any]] = []
        failed = 0

        def run(op: str) -> None:
            nonlocal failed
            res = ontology_service.apply(op)
            steps.append({"op": op, "ok": bool(res.get("ok")), "error": (res.get("error") or "")[:200]})
            if not res.get("ok"):
                failed += 1

        # 1. Удалить лишние типы (в порядке сущности -> связи -> атрибуты)
        for item in plan["drop"]:
            run(f"undefine {item['label']};")
        # 2. Добавить недостающие типы и их связи
        for item in plan["add"]:
            kind, label = item["kind"], item["label"]
            if kind == "attribute":
                run(f"define\n  attribute {label}, value {item.get('value') or 'string'};")
            elif kind == "entity":
                run(f"define\n  entity {label};")
            elif kind == "relation":
                roles = item.get("roles") or []
                lines = [f"  relation {label};"] + [f"  relation {label} relates {r};" for r in roles]
                run("define\n" + "\n".join(lines))
            # owns/plays нового типа — отдельными шагами ниже (их видно в плане)
            for attr in item.get("owns") or []:
                run(f"define\n  {kind} {label} owns {attr};")
            for role in item.get("plays") or []:
                run(f"define\n  entity {label} plays {role};")
        # 3. Дозаполнить owns/plays у существующих типов
        for item in plan["add_owns"]:
            run(f"define\n  {item['kind']} {item['type']} owns {item['attr']};")
        for item in plan["add_plays"]:
            run(f"define\n  entity {item['type']} plays {item['role']};")

        # 4. Записать откат в журнал (даже частичный — это событие)
        after = snapshot_of(ontology_service.get_schema())
        left = diff_snapshots(after, target)
        text = "\n".join(s["op"].replace("\n", " ").strip() for s in steps)
        rec = self.record(
            author=author,
            action="restore",
            define_text=text,
            comment=comment or f"откат к версии {version_id[:8]}",
            ok=(failed == 0 and left["empty"]),
            error="" if failed == 0 else f"не выполнено шагов: {failed}",
            restored_from=version_id,
            snapshot=after,
        )

        return {
            "ok": failed == 0 and left["empty"],
            "version_id": rec.get("id", ""),
            "steps": steps,
            "failed": failed,
            "unresolved": left["unresolved"],
            "ntypes": after.get("ntypes"),
            "message": (
                "схема приведена к версии" if failed == 0 and left["empty"]
                else f"частично: {failed} шагов с ошибкой, {len(left['unresolved'])} расхождений снять нельзя"
            ),
        }

    def latest_version(self) -> Optional[Dict[str, Any]]:
        """Последняя удачная версия в журнале — точка, от которой считаются отмена и повтор."""
        Session = get_session_local()
        db = Session()
        try:
            row = (db.query(OntologySchemaVersion)
                   .filter(OntologySchemaVersion.ok.is_(True))
                   .order_by(OntologySchemaVersion.created_at.desc())
                   .first())
            return row.to_dict() if row else None
        finally:
            db.close()

    def previous_version(self, current_id: str = "") -> Optional[Dict[str, Any]]:
        """Ближайшая удачная версия ДО указанной (или до последней) — для «Отменить»."""
        Session = get_session_local()
        db = Session()
        try:
            q = db.query(OntologySchemaVersion).filter(OntologySchemaVersion.ok.is_(True))
            if current_id:
                cur = db.get(OntologySchemaVersion, current_id)
                if cur:
                    q = q.filter(OntologySchemaVersion.created_at < cur.created_at)
            row = q.order_by(OntologySchemaVersion.created_at.desc()).first()
            return row.to_dict() if row else None
        finally:
            db.close()

    def next_version(self, current_id: str) -> Optional[Dict[str, Any]]:
        """Ближайшая удачная версия ПОСЛЕ указанной — для «Повторить»."""
        Session = get_session_local()
        db = Session()
        try:
            cur = db.get(OntologySchemaVersion, current_id)
            if not cur:
                return None
            row = (db.query(OntologySchemaVersion)
                   .filter(OntologySchemaVersion.ok.is_(True))
                   .filter(OntologySchemaVersion.created_at > cur.created_at)
                   .order_by(OntologySchemaVersion.created_at.asc())
                   .first())
            return row.to_dict() if row else None
        finally:
            db.close()


    def note_external_if_changed(self, schema: Dict[str, Any], author: str = "вне TypeDB-UI") -> Optional[Dict[str, Any]]:
        """Отметить в журнале правку схемы, сделанную МИМО интерфейса.

        Зачем: схему меняют и скриптами (миграции, backfill). Без этой записи журнал
        расходится с базой, и «страница показывает не то» — не баг отображения, а
        отсутствие записи. Сравнение идёт по структуре, а не по тексту: форматирование
        не считается изменением.
        """
        try:
            cur = snapshot_of(schema)
            fp = ontology_service.fingerprint(schema)
            Session = get_session_local()
            db = Session()
            try:
                last = (db.query(OntologySchemaVersion)
                        .order_by(OntologySchemaVersion.created_at.desc()).first())
                if last is None:
                    rec = OntologySchemaVersion(
                        id=uuid.uuid4().hex, author=author, action="snapshot",
                        comment=f"исходное состояние, fp={fp}", define_text="",
                        snapshot_json=json.dumps(cur, ensure_ascii=False),
                        ntypes=cur.get("ntypes", 0), ok=True,
                    )
                    db.add(rec)
                    db.commit()
                    return rec.to_dict()
                try:
                    prev = json.loads(last.snapshot_json or "{}")
                except ValueError:
                    prev = {}
                if prev == cur:
                    return None
                plan = diff_snapshots(prev, cur)
                added = [x["label"] for x in plan.get("add") or []]
                dropped = [x["label"] for x in plan.get("drop") or []]
                retyped = [f"{x['type']} +{x['attr']}" for x in plan.get("add_owns") or []]
                roles = [f"{x['type']} +{x['role']}" for x in plan.get("add_plays") or []]
                parts = []
                if added:
                    parts.append("добавлено: " + ", ".join(added))
                if dropped:
                    parts.append("удалено: " + ", ".join(dropped))
                if retyped:
                    parts.append("новые owns: " + ", ".join(retyped))
                if roles:
                    parts.append("новые plays: " + ", ".join(roles))
                rec = OntologySchemaVersion(
                    id=uuid.uuid4().hex, author=author, action="external",
                    comment=("схема изменена вне UI; " + "; ".join(parts))[:900] + f" [fp={fp}]",
                    define_text=ontology_service.live_schema_text(),
                    snapshot_json=json.dumps(cur, ensure_ascii=False),
                    ntypes=cur.get("ntypes", 0), ok=True,
                )
                db.add(rec)
                db.commit()
                logger.info("онтология: зафиксировано внешнее изменение схемы %s", fp)
                return rec.to_dict()
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 — журнал не должен ломать чтение схемы
            logger.warning("онтология: не удалось записать внешнее изменение: %s", str(exc)[:200])
            return None


ontology_versions = OntologyVersionService()
