"""contests: teams, problems, runs

Revision ID: 5c8e1f3a7b20
Revises: 2a7c5e9b0d16
Create Date: 2026-10-30 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '5c8e1f3a7b20'
down_revision: str | None = '2a7c5e9b0d16'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Только новые таблицы, существующие строки не трогаем — репетиция на
    # копии боевой базы не нужна.
    op.create_table(
        "contests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rules", sa.String(16), nullable=False),
        sa.Column("team_size", sa.Integer(), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("freeze_min", sa.Integer(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "contest_problems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contest_id", sa.Integer(), nullable=False),
        sa.Column("problem_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["contest_id"], ["contests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["problem_id"], ["problems.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("contest_id", "problem_id", name="uq_contest_problem"),
    )
    op.create_index("ix_contest_problems_contest_id", "contest_problems", ["contest_id"])
    op.create_table(
        "contest_teams",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contest_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("captain_id", sa.Integer(), nullable=True),
        sa.Column("join_code", sa.String(16), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contest_id"], ["contests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["captain_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("contest_id", "name", name="uq_contest_team_name"),
    )
    op.create_index("ix_contest_teams_contest_id", "contest_teams", ["contest_id"])
    op.create_table(
        "contest_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contest_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contest_id"], ["contests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["contest_teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("contest_id", "user_id", name="uq_contest_member"),
    )
    op.create_index("ix_contest_members_team_id", "contest_members", ["team_id"])
    op.create_index("ix_contest_members_user_id", "contest_members", ["user_id"])
    op.create_table(
        "contest_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contest_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("problem_id", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(80), nullable=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("verdict", sa.String(80), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("compile_error", sa.Boolean(), nullable=False),
        sa.Column("passed_tests", sa.Integer(), nullable=False),
        sa.Column("total_tests", sa.Integer(), nullable=False),
        sa.Column("practice", sa.Boolean(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contest_id"], ["contests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["contest_teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["problem_id"], ["problems.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_contest_runs_contest_id", "contest_runs", ["contest_id"])
    op.create_index("ix_contest_runs_team_id", "contest_runs", ["team_id"])
    op.create_index("ix_contest_runs_submitted_at", "contest_runs", ["submitted_at"])


def downgrade() -> None:
    op.drop_table("contest_runs")
    op.drop_table("contest_members")
    op.drop_table("contest_teams")
    op.drop_table("contest_problems")
    op.drop_table("contests")
