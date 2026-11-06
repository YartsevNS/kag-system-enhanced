"""Очередь предложений онтологии: чтение, проверка человеком, учёт решений.

Зачем отдельно от TypeDB: непроверенное извлечение не должно быть видно
TypeQL-правилам. Если держать предложения в онтологии со статусом, любое
правило обязано фильтровать статус — и одного забытого фильтра хватит, чтобы
система вывела нарушение по мусору. Здесь это невозможно физически: пока
status != 'accepted', в TypeDB ничего нет.

Таблица создаётся идемпотентно (CREATE TABLE IF NOT EXISTS) — сервис годится
и для стенда, и для чистой установки у заказчика.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# insufficient_context: утверждение верное, но в цитате нет смысла для вывода
# (обрезана таблица, ссылка «согласно п. X», нет условий применения) — нужна
# не правка текста, а расширение окна нарезки. Отдельный статус, чтобы такие
# случаи не терялись среди отклонённых: по ним чинится чанкер.
ALLOWED_STATUSES = ("proposed", "accepted", "rejected", "needs_schema", "insufficient_context")

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS ontology_proposals (
        id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        document_id        text        NOT NULL,
        chunk_id           text        NOT NULL,
        clause             text        NOT NULL DEFAULT '',
        page               integer     NOT NULL DEFAULT 0,
        kind               text        NOT NULL DEFAULT '',
        measure_code       text        NOT NULL DEFAULT '',
        levels             text        NOT NULL DEFAULT '',
        quote              text        NOT NULL DEFAULT '',
        requirement_id     text        NOT NULL DEFAULT '',
        text               text        NOT NULL,
        obligation_level   text        NOT NULL DEFAULT '',
        scopes             jsonb       NOT NULL DEFAULT '[]'::jsonb,
        params             jsonb       NOT NULL DEFAULT '{}'::jsonb,
        confidence         numeric,
        source_origin      text        NOT NULL DEFAULT 'regulator',
        status             text        NOT NULL DEFAULT 'proposed',
        reviewer           text,
        reviewed_at        timestamptz,
        review_note        text,
        typedb_fact_id     text,
        write_error        text,
        -- машинная проверка ансамблем моделей (HITL-предфильтр): подсказка
        -- человеку, а не решение. Решение по-прежнему ставит человек.
        verdict            text,
        verdict_model      text,
        verdict_reason     text,
        verdict_confidence numeric,
        evidence_ok        boolean,
        created_at         timestamptz NOT NULL DEFAULT now(),
        extraction_version text        NOT NULL DEFAULT 'pilot-1'
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ontology_proposals_dedup
        ON ontology_proposals (chunk_id, requirement_id, md5(text))
    """,
    """
    CREATE INDEX IF NOT EXISTS ontology_proposals_status_idx
        ON ontology_proposals (status, document_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ontology_proposals_scopes_idx
        ON ontology_proposals USING gin (scopes)
    """,
    # Миграции: таблица уже живёт на стенде, CREATE TABLE её не изменит.
    """ALTER TABLE ontology_proposals ADD COLUMN IF NOT EXISTS verdict text""",
    """ALTER TABLE ontology_proposals ADD COLUMN IF NOT EXISTS verdict_model text""",
    """ALTER TABLE ontology_proposals ADD COLUMN IF NOT EXISTS verdict_reason text""",
    """ALTER TABLE ontology_proposals ADD COLUMN IF NOT EXISTS verdict_confidence numeric""",
    """ALTER TABLE ontology_proposals ADD COLUMN IF NOT EXISTS evidence_ok boolean""",
]

_SELECT_FIELDS = """
    id, document_id, chunk_id, clause, page, kind, measure_code, levels, quote,
    requirement_id, text, obligation_level, scopes, params, confidence,
    source_origin, status, reviewer, reviewed_at, review_note, typedb_fact_id,
    write_error, created_at, extraction_version,
    verdict, verdict_model, verdict_reason, verdict_confidence, evidence_ok
"""


class ProposalService:
    """Тонкий слой SQL над очередью предложений. Схема создаётся лениво."""

    def __init__(self, db_url: str | None = None) -> None:
        self._db_url = db_url
        self._engine: Engine | None = None
        self._ready = False

    # ── подключение ──────────────────────────────────────────────────────
    def _eng(self) -> Engine:
        if self._engine is None:
            url = self._db_url or os.environ.get("KAG_DB_URL", "")
            if not url:
                raise RuntimeError("KAG_DB_URL не задан — очередь предложений недоступна")
            self._engine = create_engine(url, pool_pre_ping=True, pool_size=3, future=True)
        return self._engine

    def ensure_table(self) -> None:
        if self._ready:
            return
        with self._eng().begin() as conn:
            for stmt in _DDL:
                conn.execute(text(stmt))
        self._ready = True

    # ── чтение ───────────────────────────────────────────────────────────
    def list_proposals(
        self,
        status: str | None = "proposed",
        document_id: str | None = None,
        kind: str | None = None,
        query: str | None = None,
        scope: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.ensure_table()
        where, params = [], {}
        if status and status != "all":
            where.append("status = :status")
            params["status"] = status
        if document_id:
            where.append("document_id = :document_id")
            params["document_id"] = document_id
        if kind:
            where.append("kind = :kind")
            params["kind"] = kind
        if scope:
            where.append("scopes @> CAST(:scope AS jsonb)")
            params["scope"] = f'["{scope}"]'
        if query:
            where.append("(text ILIKE :q OR quote ILIKE :q OR requirement_id ILIKE :q)")
            params["q"] = f"%{query}%"

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(int(limit), 500))
        params.update({"limit": limit, "offset": max(0, int(offset))})

        with self._eng().connect() as conn:
            total = conn.execute(
                text(f"SELECT count(*) FROM ontology_proposals {clause}"), params
            ).scalar_one()
            rows = conn.execute(
                text(
                    f"""SELECT {_SELECT_FIELDS} FROM ontology_proposals {clause}
                        ORDER BY document_id, clause, requirement_id
                        LIMIT :limit OFFSET :offset"""
                ),
                params,
            ).mappings().all()
        return {"total": total, "items": [dict(r) for r in rows], "limit": limit, "offset": params["offset"]}

    def stats(self) -> dict[str, Any]:
        self.ensure_table()
        with self._eng().connect() as conn:
            by_status = dict(conn.execute(
                text("SELECT status, count(*) FROM ontology_proposals GROUP BY status")
            ).all())
            by_doc = conn.execute(
                text("""SELECT document_id, count(*) AS total,
                           count(*) FILTER (WHERE status='accepted') AS accepted,
                           count(*) FILTER (WHERE status='proposed') AS proposed
                        FROM ontology_proposals GROUP BY document_id ORDER BY total DESC""")
            ).mappings().all()
            by_scope = conn.execute(
                text("""SELECT s AS scope, count(*) AS n
                        FROM ontology_proposals, jsonb_array_elements_text(scopes) AS s
                        GROUP BY s ORDER BY n DESC LIMIT 20""")
            ).mappings().all()
            by_kind = dict(conn.execute(
                text("SELECT kind, count(*) FROM ontology_proposals GROUP BY kind")
            ).all())
        return {
            "by_status": by_status,
            "by_document": [dict(r) for r in by_doc],
            "by_scope": [dict(r) for r in by_scope],
            "by_kind": by_kind,
            "total": sum(by_status.values()),
        }

    def documents(self) -> list[dict[str, Any]]:
        self.ensure_table()
        with self._eng().connect() as conn:
            rows = conn.execute(text(
                """SELECT document_id, count(*) AS n FROM ontology_proposals
                   GROUP BY document_id ORDER BY document_id"""
            )).mappings().all()
        return [dict(r) for r in rows]

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        self.ensure_table()
        with self._eng().connect() as conn:
            row = conn.execute(
                text(f"SELECT {_SELECT_FIELDS} FROM ontology_proposals WHERE id = :id"),
                {"id": proposal_id},
            ).mappings().first()
        return dict(row) if row else None

    # ── решения человека ─────────────────────────────────────────────────
    def review(
        self,
        proposal_id: str,
        status: str,
        reviewer: str,
        note: str | None = None,
        new_text: str | None = None,
        typedb_fact_id: str | None = None,
        write_error: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"недопустимый статус: {status}")
        self.ensure_table()
        with self._eng().begin() as conn:
            row = conn.execute(
                text(
                    """UPDATE ontology_proposals SET
                           status = :status,
                           reviewer = :reviewer,
                           reviewed_at = now(),
                           review_note = :note,
                           text = COALESCE(:new_text, text),
                           typedb_fact_id = COALESCE(:fact_id, typedb_fact_id),
                           write_error = :write_error
                       WHERE id = :id
                       RETURNING id, status, reviewer, reviewed_at"""
                ),
                {"id": proposal_id, "status": status, "reviewer": reviewer, "note": note,
                 "new_text": new_text, "fact_id": typedb_fact_id, "write_error": write_error},
            ).mappings().first()
        return dict(row) if row else None

    def bulk_review(
        self, ids: Iterable[str], status: str, reviewer: str, note: str | None = None
    ) -> int:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"недопустимый статус: {status}")
        ids = [str(i) for i in ids]
        if not ids:
            return 0
        self.ensure_table()
        with self._eng().begin() as conn:
            res = conn.execute(
                text(
                    """UPDATE ontology_proposals SET
                           status = :status, reviewer = :reviewer,
                           reviewed_at = now(), review_note = :note
                       WHERE id = ANY(CAST(:ids AS uuid[])) AND status <> 'accepted'
                       RETURNING id"""
                ),
                {"ids": ids, "status": status, "reviewer": reviewer, "note": note},
            )
            return len(res.fetchall())


proposal_service = ProposalService()
