"""ORM models of every table Bianque owns.

The schema itself is created by the Alembic migrations in ``migrations/``, never by
``metadata.create_all``; a test asserts that these models and the migrated database
agree. LangGraph checkpoint tables live in the separate ``langgraph`` schema and are
managed by the checkpoint library, not here.

Conventions:

- Enumerations are ``text`` columns with a ``CHECK`` constraint, so adding a value only
  changes the constraint.
- Investigation, evidence, suggestion and knowledge chunk IDs are text (see
  ``bianque.storage.ids``); every other table uses a ``bigint`` identity key.
- Structured fields are ``jsonb``; timestamps are ``timestamptz``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Dimension of the local embedding model's vectors.
EMBEDDING_DIMENSION = 384

#: Constraint names follow one convention so that the migration and the models agree.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

INVESTIGATION_STATUSES = ("pending", "running", "reported", "degraded", "interrupted", "failed")
#: Statuses of an investigation that has not ended yet.
OPEN_INVESTIGATION_STATUSES = ("pending", "running")


def _in(column: str, values: tuple[str, ...]) -> str:
    """Build the SQL of a ``CHECK`` constraint that limits a column to fixed values."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _bigint_id() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _json_list() -> Mapped[list[Any]]:
    return mapped_column(JSONB, server_default=text("'[]'::jsonb"))


def _json_object() -> Mapped[dict[str, Any]]:
    return mapped_column(JSONB, server_default=text("'{}'::jsonb"))


def _investigation_fk(*, cascade: bool = True) -> Mapped[str]:
    return mapped_column(
        Text, ForeignKey("investigations.id", ondelete="CASCADE" if cascade else None)
    )


class Base(DeclarativeBase):
    """Declarative base sharing one ``MetaData`` with the naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Alert(Base):
    """An alert as received from Alertmanager."""

    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint(_in("status", ("firing", "resolved")), name="status"),
        Index(None, "fingerprint"),
    )

    id: Mapped[int] = _bigint_id()
    fingerprint: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    labels: Mapped[dict[str, Any]] = _json_object()
    annotations: Mapped[dict[str, Any]] = _json_object()
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Investigation(Base):
    """One run of the investigation graph for an alert."""

    __tablename__ = "investigations"
    __table_args__ = (
        CheckConstraint(_in("trigger", ("alert", "manual", "replay")), name="trigger"),
        CheckConstraint(_in("status", INVESTIGATION_STATUSES), name="status"),
        CheckConstraint(_in("tool_backend", ("live", "pack")), name="tool_backend"),
        CheckConstraint(_in("llm_mode", ("live", "recorded", "scripted")), name="llm_mode"),
        Index(None, "status"),
        Index(None, "started_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    alert_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("alerts.id"))
    trigger: Mapped[str] = mapped_column(Text)
    case_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    tool_backend: Mapped[str] = mapped_column(Text)
    llm_mode: Mapped[str] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(Text)
    token_budget: Mapped[int] = mapped_column(Integer)
    tokens_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Plan(Base):
    """One version of an investigation plan; each replan adds a version."""

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(_in("source", ("model", "default")), name="source"),
        UniqueConstraint("investigation_id", "version"),
    )

    id: Mapped[int] = _bigint_id()
    investigation_id: Mapped[str] = _investigation_fk()
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Step(Base):
    """One step of a plan, carried out by an evidence-collection sub-agent."""

    __tablename__ = "steps"
    __table_args__ = (
        CheckConstraint(_in("status", ("pending", "running", "done", "failed")), name="status"),
        UniqueConstraint("plan_id", "seq"),
    )

    id: Mapped[int] = _bigint_id()
    plan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("plans.id", ondelete="CASCADE"))
    investigation_id: Mapped[str] = _investigation_fk()
    seq: Mapped[int] = mapped_column(Integer)
    domain: Mapped[str] = mapped_column(Text)
    goal: Mapped[str] = mapped_column(Text)
    suggested_tools: Mapped[list[Any]] = _json_list()
    expected_evidence: Mapped[list[Any]] = _json_list()
    agent: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    findings: Mapped[list[Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Assessment(Base):
    """The judgement after one round: whether the evidence is sufficient."""

    __tablename__ = "assessments"
    __table_args__ = (UniqueConstraint("investigation_id", "round"),)

    id: Mapped[int] = _bigint_id()
    investigation_id: Mapped[str] = _investigation_fk()
    round: Mapped[int] = mapped_column(Integer)
    sufficient: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    hypothesis_category: Mapped[str | None] = mapped_column(Text)
    missing: Mapped[list[Any]] = _json_list()
    rule_override: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()


class CallRecord(Base):
    """One model, tool or knowledge call, for the call timeline and cost totals."""

    __tablename__ = "call_records"
    __table_args__ = (
        CheckConstraint(_in("kind", ("model", "tool", "knowledge")), name="kind"),
        Index(None, "investigation_id", "started_at"),
        Index(None, "session_id"),
    )

    id: Mapped[int] = _bigint_id()
    investigation_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("investigations.id", ondelete="CASCADE")
    )
    session_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("followup_sessions.id", ondelete="CASCADE")
    )
    agent: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    caller: Mapped[str | None] = mapped_column(Text)
    input_summary: Mapped[str] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    gate_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    retries: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)


class Evidence(Base):
    """A piece of evidence; its ID is derived from content and unique per investigation."""

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(_in("source", ("alertmanager", "kubernetes", "git")), name="source"),
    )

    investigation_id: Mapped[str] = mapped_column(
        Text, ForeignKey("investigations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    step_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("steps.id"))
    call_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("call_records.id"))
    source: Mapped[str] = mapped_column(Text)
    tool: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_content: Mapped[str] = mapped_column(Text)
    redacted_content: Mapped[str] = mapped_column(Text)
    identifiers: Mapped[list[Any]] = _json_list()
    meta: Mapped[dict[str, Any]] = _json_object()


class InvestigationEvent(Base):
    """A persisted progress event, replayed to SSE clients that reconnect."""

    __tablename__ = "investigation_events"

    investigation_id: Mapped[str] = mapped_column(
        Text, ForeignKey("investigations.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = _json_object()
    created_at: Mapped[datetime] = _created_at()


class Report(Base):
    """One attempt at the investigation report; rejected attempts are kept too."""

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(_in("status", ("published", "rejected", "degraded")), name="status"),
        UniqueConstraint("investigation_id", "attempt"),
    )

    id: Mapped[int] = _bigint_id()
    investigation_id: Mapped[str] = _investigation_fk()
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    gate_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    schema_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Suggestion(Base):
    """A remediation suggestion of a published report; never modified afterwards."""

    __tablename__ = "suggestions"
    __table_args__ = (
        CheckConstraint(_in("risk", ("read_only", "change")), name="risk"),
        UniqueConstraint("investigation_id", "report_id", "seq"),
    )

    investigation_id: Mapped[str] = mapped_column(
        Text, ForeignKey("investigations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    report_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("reports.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    command: Mapped[str | None] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[Any]] = _json_list()


class Confirmation(Base):
    """A human decision on a suggestion. Append-only: a trigger rejects UPDATE and DELETE.

    A suggestion is pending, confirmed or rejected depending on whether a confirmation
    exists and what it says; suggestions have no status column of their own.
    """

    __tablename__ = "confirmations"
    __table_args__ = (
        CheckConstraint(_in("decision", ("confirmed", "rejected")), name="decision"),
        ForeignKeyConstraint(
            ["investigation_id", "suggestion_id"],
            ["suggestions.investigation_id", "suggestions.id"],
        ),
        UniqueConstraint("investigation_id", "suggestion_id"),
    )

    id: Mapped[int] = _bigint_id()
    investigation_id: Mapped[str] = _investigation_fk(cascade=False)
    suggestion_id: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FollowupSession(Base):
    """A follow-up conversation about one investigation."""

    __tablename__ = "followup_sessions"

    id: Mapped[int] = _bigint_id()
    investigation_id: Mapped[str] = _investigation_fk()
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class FollowupMessage(Base):
    """A message of a follow-up session; only answers that passed validation are stored."""

    __tablename__ = "followup_messages"
    __table_args__ = (
        CheckConstraint(_in("role", ("user", "assistant")), name="role"),
        UniqueConstraint("session_id", "seq"),
    )

    id: Mapped[int] = _bigint_id()
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("followup_sessions.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    #: The paragraphs of an answer: ``{text, citations, insufficient_evidence}`` each.
    segments: Mapped[list[Any]] = _json_list()
    citations: Mapped[list[Any]] = _json_list()
    references: Mapped[list[Any]] = _json_list()
    unknown: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()


class KnowledgeDocument(Base):
    """An imported runbook file."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("path"),)

    id: Mapped[int] = _bigint_id()
    path: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


#: Full-text vector of a chunk: heading matches weigh more than body matches.
CHUNK_TSV_EXPRESSION = (
    "setweight(to_tsvector('english', heading_path), 'A') || "
    "setweight(to_tsvector('english', content), 'B')"
)


class KnowledgeChunk(Base):
    """A chunk of a runbook with its embedding and full-text vector in the same row."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal"),
        Index(None, "tsv", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_documents.id", ondelete="CASCADE")
    )
    heading_path: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION))
    tsv: Mapped[str] = mapped_column(TSVECTOR, Computed(CHUNK_TSV_EXPRESSION, persisted=True))
