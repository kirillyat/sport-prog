"""shorter titles for attendance and exam columns

Revision ID: e4c8a5219fb3
Revises: d1b6f39c2e75
Create Date: 2026-09-25 02:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'e4c8a5219fb3'
down_revision: str | None = 'd1b6f39c2e75'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Подпись стоит над клеткой шириной в значок. Что это за отметка, говорит
    # шапка занятия — «Неделя 5 · контрольная», — поэтому в колонке хватает
    # короткого слова.
    op.execute("UPDATE sheet_columns SET title = 'явка' WHERE title = 'посещение'")
    op.execute(
        "UPDATE sheet_columns SET title = 'баллы' WHERE title = 'контрольная' AND scale = 'points'"
    )


def downgrade() -> None:
    op.execute("UPDATE sheet_columns SET title = 'посещение' WHERE title = 'явка'")
    op.execute(
        "UPDATE sheet_columns SET title = 'контрольная' WHERE title = 'баллы' AND scale = 'points'"
    )
