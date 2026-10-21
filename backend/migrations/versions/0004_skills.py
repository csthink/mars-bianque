"""Skills loaded while planning.

A skill activation of the planning model is recorded as a call of kind ``skill``, and
each plan version keeps the skills that were loaded when it was generated, filled by
code, not by the model.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_call_records_kind"


def upgrade() -> None:
    """Add ``plans.skills`` and allow ``skill`` as a call kind; earlier plans loaded none."""
    op.add_column(
        "plans", sa.Column("skills", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    )
    op.drop_constraint(CONSTRAINT, "call_records", type_="check")
    op.create_check_constraint(
        CONSTRAINT, "call_records", "kind IN ('model', 'tool', 'knowledge', 'skill')"
    )


def downgrade() -> None:
    """Remove skill call records and ``plans.skills``."""
    op.execute("DELETE FROM call_records WHERE kind = 'skill'")
    op.drop_constraint(CONSTRAINT, "call_records", type_="check")
    op.create_check_constraint(CONSTRAINT, "call_records", "kind IN ('model', 'tool', 'knowledge')")
    op.drop_column("plans", "skills")
