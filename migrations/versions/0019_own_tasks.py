"""own tasks with tests

Revision ID: a2f7c31d9b84
Revises: e81b6c2d4a37
Create Date: 2026-09-24 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a2f7c31d9b84'
down_revision: str | None = 'e81b6c2d4a37'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("problem_id", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False, server_default=""),
        sa.Column("time_limit_ms", sa.Integer(), nullable=False, server_default="2000"),
        sa.Column("memory_limit_mb", sa.Integer(), nullable=False, server_default="256"),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["problem_id"], ["problems.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("problem_id"),
    )
    op.create_index("ix_tasks_problem_id", "tasks", ["problem_id"])

    op.create_table(
        "task_tests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("stdin", sa.Text(), nullable=False, server_default=""),
        sa.Column("expected", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "position", name="uq_task_test_position"),
    )
    op.create_index("ix_task_tests_task_id", "task_tests", ["task_id"])

    # Посылка своей задачи приходит прямо на портал, аккаунта площадки у неё нет.
    with op.batch_alter_table("submissions", schema=None) as batch:
        batch.alter_column("platform_account_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.drop_table("task_tests")
    op.drop_table("tasks")
    with op.batch_alter_table("submissions", schema=None) as batch:
        batch.alter_column("platform_account_id", existing_type=sa.Integer(), nullable=False)
