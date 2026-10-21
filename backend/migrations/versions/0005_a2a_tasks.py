"""A2A tasks, and ``a2a`` as an investigation trigger.

An A2A client starts an investigation with a task. ``a2a_tasks`` keeps which client
started each task, in which context, and the investigation it follows or the reason it
was rejected; the task's state is derived from the investigation, never stored.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER = "ck_investigations_trigger"
REJECTED_REASONS = (
    "malformed",
    "namespace_denied",
    "profile_mismatch",
    "unknown_case",
    "resolved_alert",
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """Allow the ``a2a`` trigger and create ``a2a_tasks``."""
    op.drop_constraint(TRIGGER, "investigations", type_="check")
    op.create_check_constraint(
        TRIGGER, "investigations", "trigger IN ('alert', 'manual', 'replay', 'a2a')"
    )
    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("context_id", sa.Text, nullable=False),
        sa.Column("investigation_id", sa.Text, nullable=True),
        sa.Column("caller", sa.Text, nullable=False),
        sa.Column("rejected_reason", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_a2a_tasks"),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name="fk_a2a_tasks_investigation_id_investigations",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            f"rejected_reason IN ({_quoted(REJECTED_REASONS)})",
            name="ck_a2a_tasks_rejected_reason",
        ),
        sa.CheckConstraint(
            "(investigation_id IS NULL) <> (rejected_reason IS NULL)", name="ck_a2a_tasks_outcome"
        ),
    )
    op.create_index("ix_a2a_tasks_caller_created_at", "a2a_tasks", ["caller", "created_at"])
    op.create_index("ix_a2a_tasks_investigation_id", "a2a_tasks", ["investigation_id"])


def downgrade() -> None:
    """Drop ``a2a_tasks``; investigations an A2A task started stay, as replays or alerts.

    They are kept rather than deleted: decisions on their suggestions reference them and
    are append-only.
    """
    op.drop_table("a2a_tasks")
    op.execute(
        "UPDATE investigations SET trigger = CASE WHEN case_id IS NULL THEN 'alert' "
        "ELSE 'replay' END WHERE trigger = 'a2a'"
    )
    op.drop_constraint(TRIGGER, "investigations", type_="check")
    op.create_check_constraint(
        TRIGGER, "investigations", "trigger IN ('alert', 'manual', 'replay')"
    )
