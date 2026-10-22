"""official name from the university account

Revision ID: 7b3f2c9e1a54
Revises: 5c1e9a7d4b20
Create Date: 2026-10-22 15:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '7b3f2c9e1a54'
down_revision: str | None = '5c1e9a7d4b20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Новая пустая колонка, существующие строки не трогаем — репетиция на
    # копии боевой базы не нужна. Заполнит её следующий вход через OIDC:
    # восстановить из display_name нельзя — его студенты уже могли поменять.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("official_name", sa.String(length=120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("official_name")
