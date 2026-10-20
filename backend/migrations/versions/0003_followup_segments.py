"""The paragraphs of follow-up answers.

A follow-up answer is a list of paragraphs, each with its own citations and its own
"insufficient evidence" flag. ``segments`` keeps them; ``content``, ``citations`` and
``unknown`` stay as the summary of the whole message. Questions have no segments.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``followup_messages.segments``; messages stored before it have none."""
    op.add_column(
        "followup_messages",
        sa.Column("segments", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    """Drop ``followup_messages.segments``; the summary columns keep the answers' text."""
    op.drop_column("followup_messages", "segments")
