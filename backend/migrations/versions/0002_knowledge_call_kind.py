"""Call records of knowledge retrieval.

The investigation's knowledge retrieval is recorded as a call of kind ``knowledge``, so
the call timeline shows which runbook chunks the agent was given.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_call_records_kind"


def upgrade() -> None:
    """Allow ``knowledge`` as a call kind."""
    op.drop_constraint(CONSTRAINT, "call_records", type_="check")
    op.create_check_constraint(CONSTRAINT, "call_records", "kind IN ('model', 'tool', 'knowledge')")


def downgrade() -> None:
    """Remove knowledge call records and allow only model and tool calls again."""
    op.execute("DELETE FROM call_records WHERE kind = 'knowledge'")
    op.drop_constraint(CONSTRAINT, "call_records", type_="check")
    op.create_check_constraint(CONSTRAINT, "call_records", "kind IN ('model', 'tool')")
