"""sheet lessons: several grades per seminar

Revision ID: c7e2a4f8d531
Revises: b5d9e0c73a12
Create Date: 2026-09-24 18:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c7e2a4f8d531'
down_revision: str | None = 'b5d9e0c73a12'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sheet_lessons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("held_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sheet_lessons_group_id", "sheet_lessons", ["group_id"])

    with op.batch_alter_table("sheet_columns") as batch:
        batch.add_column(sa.Column("lesson_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_sheet_columns_lesson", "sheet_lessons", ["lesson_id"], ["id"], ondelete="CASCADE"
        )
    op.create_index("ix_sheet_columns_lesson_id", "sheet_columns", ["lesson_id"])

    # Колонки, у которых уже была дата, становятся занятиями: дата — свойство
    # занятия, и оставлять её в двух местах нельзя. Название занятия берём
    # у колонки, сама колонка уходит внутрь него.
    op.execute(
        "INSERT INTO sheet_lessons (group_id, title, held_on, position, created_at) "
        "SELECT group_id, title, held_on, position, created_at "
        "FROM sheet_columns WHERE held_on IS NOT NULL"
    )
    op.execute(
        "UPDATE sheet_columns SET lesson_id = ("
        "  SELECT l.id FROM sheet_lessons l"
        "  WHERE l.group_id = sheet_columns.group_id"
        "    AND l.title = sheet_columns.title"
        "    AND l.held_on = sheet_columns.held_on"
        ") WHERE held_on IS NOT NULL"
    )

    with op.batch_alter_table("sheet_columns") as batch:
        batch.drop_column("held_on")


def downgrade() -> None:
    with op.batch_alter_table("sheet_columns") as batch:
        batch.add_column(sa.Column("held_on", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE sheet_columns SET held_on = ("
        "  SELECT l.held_on FROM sheet_lessons l WHERE l.id = sheet_columns.lesson_id"
        ") WHERE lesson_id IS NOT NULL"
    )
    with op.batch_alter_table("sheet_columns") as batch:
        batch.drop_constraint("fk_sheet_columns_lesson", type_="foreignkey")
        batch.drop_column("lesson_id")
    op.drop_table("sheet_lessons")
