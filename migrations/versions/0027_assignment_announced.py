"""assignment announced flag

Revision ID: 2a7c5e9b0d16
Revises: 9d4e6a1f3c08
Create Date: 2026-10-22 21:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '2a7c5e9b0d16'
down_revision: str | None = '9d4e6a1f3c08'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Новая колонка с постоянным значением по умолчанию: у всех уже выданных
    # заданий сообщение ушло при выдаче — они «объявлены», и планировщик их
    # не тронет. Строки не переписываются, репетиция на копии не нужна.
    with op.batch_alter_table("assignments") as batch:
        batch.add_column(
            sa.Column("announced", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("assignments") as batch:
        batch.drop_column("announced")
