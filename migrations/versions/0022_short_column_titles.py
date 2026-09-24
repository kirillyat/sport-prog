"""short titles for lesson columns

Revision ID: d1b6f39c2e75
Revises: c7e2a4f8d531
Create Date: 2026-09-25 01:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'd1b6f39c2e75'
down_revision: str | None = 'c7e2a4f8d531'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Подпись колонки в ведомости стоит над клеткой шириной в значок, и
    # «работа на семинаре» там всё равно обрезалась. Контекст даёт шапка
    # занятия — «Неделя 3 · семинар», — поэтому в колонке достаточно «работа».
    op.execute(
        "UPDATE sheet_columns SET title = 'работа' WHERE title = 'работа на семинаре'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE sheet_columns SET title = 'работа на семинаре' WHERE title = 'работа'"
    )
