"""Initial schema.

Explicit DDL for every table Bianque owns, the pgvector extension, the full-text
index of knowledge chunks, the append-only trigger of confirmations and the schema
that holds LangGraph checkpoint tables.

Revision ID: 0001
Revises:
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)
NOW = sa.text("now()")
EMPTY_LIST = sa.text("'[]'::jsonb")
EMPTY_OBJECT = sa.text("'{}'::jsonb")

CHUNK_TSV_EXPRESSION = (
    "setweight(to_tsvector('english', heading_path), 'A') || "
    "setweight(to_tsvector('english', content), 'B')"
)


def _in(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=f"ck_{table}_{column}")


def _pk(table: str, *columns: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint(*columns, name=f"pk_{table}")


def _uq(table: str, *columns: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint(*columns, name=f"uq_{table}_{'_'.join(columns)}")


def _fk(
    table: str,
    columns: Sequence[str],
    referred: str,
    referred_columns: Sequence[str],
    *,
    ondelete: str | None = None,
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        list(columns),
        [f"{referred}.{column}" for column in referred_columns],
        name=f"fk_{table}_{'_'.join(columns)}_{referred}",
        ondelete=ondelete,
    )


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False)


def _investigation_id(*, nullable: bool = False) -> sa.Column[str]:
    return sa.Column("investigation_id", sa.Text, nullable=nullable)


def _to_investigation(table: str, *, cascade: bool = True) -> sa.ForeignKeyConstraint:
    return _fk(
        table,
        ["investigation_id"],
        "investigations",
        ["id"],
        ondelete="CASCADE" if cascade else None,
    )


def upgrade() -> None:
    """Create the initial schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS langgraph")

    op.create_table(
        "alerts",
        _id(),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("labels", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("annotations", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("starts_at", TZ, nullable=False),
        sa.Column("received_at", TZ, nullable=False, server_default=NOW),
        sa.Column("payload", JSONB, nullable=False),
        _pk("alerts", "id"),
        _in("alerts", "status", ("firing", "resolved")),
    )
    op.create_index("ix_alerts_fingerprint", "alerts", ["fingerprint"])

    op.create_table(
        "investigations",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("alert_id", sa.BigInteger, nullable=True),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("case_id", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("tool_backend", sa.Text, nullable=False),
        sa.Column("llm_mode", sa.Text, nullable=False),
        sa.Column("llm_model", sa.Text, nullable=True),
        sa.Column("token_budget", sa.Integer, nullable=False),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", TZ, nullable=False, server_default=NOW),
        sa.Column("finished_at", TZ, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        _pk("investigations", "id"),
        _fk("investigations", ["alert_id"], "alerts", ["id"]),
        _in("investigations", "trigger", ("alert", "manual", "replay")),
        _in(
            "investigations",
            "status",
            ("pending", "running", "reported", "degraded", "interrupted", "failed"),
        ),
        _in("investigations", "tool_backend", ("live", "pack")),
        _in("investigations", "llm_mode", ("live", "recorded", "scripted")),
    )
    op.create_index("ix_investigations_status", "investigations", ["status"])
    op.create_index("ix_investigations_started_at", "investigations", ["started_at"])

    op.create_table(
        "plans",
        _id(),
        _investigation_id(),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        _pk("plans", "id"),
        _to_investigation("plans"),
        _uq("plans", "investigation_id", "version"),
        _in("plans", "source", ("model", "default")),
    )

    op.create_table(
        "steps",
        _id(),
        sa.Column("plan_id", sa.BigInteger, nullable=False),
        _investigation_id(),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("suggested_tools", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("expected_evidence", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("agent", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("findings", JSONB, nullable=True),
        sa.Column("started_at", TZ, nullable=True),
        sa.Column("finished_at", TZ, nullable=True),
        _pk("steps", "id"),
        _fk("steps", ["plan_id"], "plans", ["id"], ondelete="CASCADE"),
        _to_investigation("steps"),
        _uq("steps", "plan_id", "seq"),
        _in("steps", "status", ("pending", "running", "done", "failed")),
    )

    op.create_table(
        "assessments",
        _id(),
        _investigation_id(),
        sa.Column("round", sa.Integer, nullable=False),
        sa.Column("sufficient", sa.Boolean, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("hypothesis_category", sa.Text, nullable=True),
        sa.Column("missing", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("rule_override", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        _pk("assessments", "id"),
        _to_investigation("assessments"),
        _uq("assessments", "investigation_id", "round"),
    )

    op.create_table(
        "followup_sessions",
        _id(),
        _investigation_id(),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        _pk("followup_sessions", "id"),
        _to_investigation("followup_sessions"),
    )

    op.create_table(
        "call_records",
        _id(),
        _investigation_id(nullable=True),
        sa.Column("session_id", sa.BigInteger, nullable=True),
        sa.Column("agent", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("caller", sa.Text, nullable=True),
        sa.Column("input_summary", sa.Text, nullable=False),
        sa.Column("output_summary", sa.Text, nullable=True),
        sa.Column("gate_result", JSONB, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("retries", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", TZ, nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("tokens_in", sa.Integer, nullable=True),
        sa.Column("tokens_out", sa.Integer, nullable=True),
        _pk("call_records", "id"),
        _to_investigation("call_records"),
        _fk("call_records", ["session_id"], "followup_sessions", ["id"], ondelete="CASCADE"),
        _in("call_records", "kind", ("model", "tool")),
    )
    op.create_index(
        "ix_call_records_investigation_id_started_at",
        "call_records",
        ["investigation_id", "started_at"],
    )
    op.create_index("ix_call_records_session_id", "call_records", ["session_id"])

    op.create_table(
        "evidence",
        _investigation_id(),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("step_id", sa.BigInteger, nullable=True),
        sa.Column("call_id", sa.BigInteger, nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("tool", sa.Text, nullable=True),
        sa.Column("collected_at", TZ, nullable=False),
        sa.Column("raw_content", sa.Text, nullable=False),
        sa.Column("redacted_content", sa.Text, nullable=False),
        sa.Column("identifiers", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("meta", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        _pk("evidence", "investigation_id", "id"),
        _to_investigation("evidence"),
        _fk("evidence", ["step_id"], "steps", ["id"]),
        _fk("evidence", ["call_id"], "call_records", ["id"]),
        _in("evidence", "source", ("alertmanager", "kubernetes", "git")),
    )

    op.create_table(
        "investigation_events",
        _investigation_id(),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=EMPTY_OBJECT),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        _pk("investigation_events", "investigation_id", "seq"),
        _to_investigation("investigation_events"),
    )

    op.create_table(
        "reports",
        _id(),
        _investigation_id(),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("body", JSONB, nullable=False),
        sa.Column("gate_result", JSONB, nullable=True),
        sa.Column("schema_version", sa.Text, nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        _pk("reports", "id"),
        _to_investigation("reports"),
        _uq("reports", "investigation_id", "attempt"),
        _in("reports", "status", ("published", "rejected", "degraded")),
    )

    op.create_table(
        "suggestions",
        _investigation_id(),
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("report_id", sa.BigInteger, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("command", sa.Text, nullable=True),
        sa.Column("risk", sa.Text, nullable=False),
        sa.Column("citations", JSONB, nullable=False, server_default=EMPTY_LIST),
        _pk("suggestions", "investigation_id", "id"),
        _to_investigation("suggestions"),
        _fk("suggestions", ["report_id"], "reports", ["id"], ondelete="CASCADE"),
        _uq("suggestions", "investigation_id", "report_id", "seq"),
        _in("suggestions", "risk", ("read_only", "change")),
    )

    op.create_table(
        "confirmations",
        _id(),
        _investigation_id(),
        sa.Column("suggestion_id", sa.Text, nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("decided_at", TZ, nullable=False, server_default=NOW),
        _pk("confirmations", "id"),
        _to_investigation("confirmations", cascade=False),
        _fk(
            "confirmations",
            ["investigation_id", "suggestion_id"],
            "suggestions",
            ["investigation_id", "id"],
        ),
        _uq("confirmations", "investigation_id", "suggestion_id"),
        _in("confirmations", "decision", ("confirmed", "rejected")),
    )
    op.execute(
        """
        CREATE FUNCTION confirmations_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'confirmations is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER confirmations_append_only
        BEFORE UPDATE OR DELETE ON confirmations
        FOR EACH ROW EXECUTE FUNCTION confirmations_append_only()
        """
    )

    op.create_table(
        "followup_messages",
        _id(),
        sa.Column("session_id", sa.BigInteger, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("citations", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("references", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("unknown", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", TZ, nullable=False, server_default=NOW),
        _pk("followup_messages", "id"),
        _fk("followup_messages", ["session_id"], "followup_sessions", ["id"], ondelete="CASCADE"),
        _uq("followup_messages", "session_id", "seq"),
        _in("followup_messages", "role", ("user", "assistant")),
    )

    op.create_table(
        "knowledge_documents",
        _id(),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("ingested_at", TZ, nullable=False, server_default=NOW),
        _pk("knowledge_documents", "id"),
        _uq("knowledge_documents", "path"),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("document_id", sa.BigInteger, nullable=False),
        sa.Column("heading_path", sa.Text, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column(
            "tsv", TSVECTOR, sa.Computed(CHUNK_TSV_EXPRESSION, persisted=True), nullable=False
        ),
        _pk("knowledge_chunks", "id"),
        _fk(
            "knowledge_chunks",
            ["document_id"],
            "knowledge_documents",
            ["id"],
            ondelete="CASCADE",
        ),
        _uq("knowledge_chunks", "document_id", "ordinal"),
    )
    op.create_index("ix_knowledge_chunks_tsv", "knowledge_chunks", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    """Drop the initial schema; the vector extension is left in place."""
    for table in (
        "knowledge_chunks",
        "knowledge_documents",
        "followup_messages",
        "confirmations",
        "suggestions",
        "reports",
        "investigation_events",
        "evidence",
        "call_records",
        "followup_sessions",
        "assessments",
        "steps",
        "plans",
        "investigations",
        "alerts",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION confirmations_append_only()")
    op.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")
