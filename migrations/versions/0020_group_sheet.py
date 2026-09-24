"""group sheet: columns and marks

Revision ID: b5d9e0c73a12
Revises: a2f7c31d9b84
Create Date: 2026-09-24 16:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b5d9e0c73a12'
down_revision: str | None = 'a2f7c31d9b84'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sheet_columns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("scale", sa.String(length=20), nullable=False, server_default="pass_fail"),
        sa.Column("max_points", sa.Float(), nullable=False, server_default="1"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assignment_id", sa.Integer(), nullable=True),
        sa.Column("required_solved", sa.Integer(), nullable=True),
        sa.Column("held_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sheet_columns_group_id", "sheet_columns", ["group_id"])

    op.create_table(
        "sheet_marks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("column_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("points", sa.Float(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("attendance", sa.String(length=20), nullable=True),
        sa.Column("comment", sa.String(length=500), nullable=True),
        sa.Column("graded_by_id", sa.Integer(), nullable=True),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["column_id"], ["sheet_columns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["graded_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("column_id", "user_id", name="uq_sheet_mark"),
    )
    op.create_index("ix_sheet_marks_column_id", "sheet_marks", ["column_id"])
    op.create_index("ix_sheet_marks_user_id", "sheet_marks", ["user_id"])

    # Раздел появляется закрытым для студентов: пока ведомость наполняется,
    # показывать её незачем. Обычно отсутствие строки означает «открыто всем»,
    # поэтому запрет приходится записать явно — преподаватель снимет его
    # в «Видимости разделов», когда решит, что пора.
    op.execute(
        "INSERT INTO feature_flags (key, for_students, for_teachers, updated_at) "
        "VALUES ('grades', 0, 1, CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM feature_flags WHERE key = 'grades'")
    op.drop_table("sheet_marks")
    op.drop_table("sheet_columns")
