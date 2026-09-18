"""drop hard deadline flag

Revision ID: c3a5d21b7f40
Revises: b7c41f0a9e12
Create Date: 2026-09-18 18:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c3a5d21b7f40'
down_revision: str | None = 'b7c41f0a9e12'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Флаг различал «после дедлайна — половина баллов» и «после дедлайна —
    # ноль». Половины больше нет ни у кого, поэтому различать нечего.
    with op.batch_alter_table("assignments", schema=None) as batch:
        batch.drop_column("hard_deadline")


def downgrade() -> None:
    with op.batch_alter_table("assignments", schema=None) as batch:
        batch.add_column(
            sa.Column("hard_deadline", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
