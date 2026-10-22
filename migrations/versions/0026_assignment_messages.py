"""assignment messages from the teacher

Revision ID: 9d4e6a1f3c08
Revises: 7b3f2c9e1a54
Create Date: 2026-10-22 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '9d4e6a1f3c08'
down_revision: str | None = '7b3f2c9e1a54'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Новая таблица, существующие строки не трогаем — репетиция на копии
    # боевой базы не нужна.
    op.create_table(
        "assignment_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_assignment_messages_assignment_id", "assignment_messages", ["assignment_id"])
    op.create_index("ix_assignment_messages_created_at", "assignment_messages", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_assignment_messages_created_at", table_name="assignment_messages")
    op.drop_index("ix_assignment_messages_assignment_id", table_name="assignment_messages")
    op.drop_table("assignment_messages")
