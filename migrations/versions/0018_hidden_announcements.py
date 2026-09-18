"""hidden announcements

Revision ID: e81b6c2d4a37
Revises: c3a5d21b7f40
Create Date: 2026-09-18 18:45:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e81b6c2d4a37'
down_revision: str | None = 'c3a5d21b7f40'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hidden_announcements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("announcement_id", sa.Integer(), nullable=False),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["announcement_id"], ["announcements.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "announcement_id", name="uq_hidden_announcement"),
    )
    op.create_index("ix_hidden_announcements_user_id", "hidden_announcements", ["user_id"])
    op.create_index(
        "ix_hidden_announcements_announcement_id", "hidden_announcements", ["announcement_id"]
    )


def downgrade() -> None:
    op.drop_table("hidden_announcements")
