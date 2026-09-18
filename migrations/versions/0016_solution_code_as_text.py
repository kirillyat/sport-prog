"""solution code as text

Revision ID: b7c41f0a9e12
Revises: d2873dff92b3
Create Date: 2026-09-18 18:10:00.000000
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = 'b7c41f0a9e12'
down_revision: str | None = 'd2873dff92b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _storage() -> Path:
    """Где лежали файлы решений. DATA_DIR читаем сами: настройки тут не нужны."""
    return Path(os.environ.get("DATA_DIR", "./data")) / "solutions"


def upgrade() -> None:
    with op.batch_alter_table("solution_uploads", schema=None) as batch:
        batch.add_column(sa.Column("code", sa.Text(), nullable=False, server_default=""))

    # Переносим то, что уже прислали файлами. Файла нет или он не читается
    # текстом — оставляем пометку вместо кода: терять строку проверки хуже,
    # чем показать преподавателю, что содержимое потерялось.
    bind = op.get_bind()
    rows = bind.execute(sa.text("select id, stored_name, filename from solution_uploads")).all()
    for row_id, stored_name, filename in rows:
        path = _storage() / str(stored_name or "")
        try:
            code = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            code = f"# файл {filename!r} не перенёсся: решение нужно прислать заново"
        bind.execute(
            sa.text("update solution_uploads set code = :code where id = :id"),
            {"code": code, "id": row_id},
        )

    with op.batch_alter_table("solution_uploads", schema=None) as batch:
        batch.drop_column("filename")
        batch.drop_column("stored_name")
        batch.drop_column("size")


def downgrade() -> None:
    with op.batch_alter_table("solution_uploads", schema=None) as batch:
        batch.add_column(sa.Column("filename", sa.String(200), nullable=False, server_default=""))
        batch.add_column(sa.Column("stored_name", sa.String(80), nullable=False, server_default=""))
        batch.add_column(sa.Column("size", sa.Integer(), nullable=False, server_default="0"))
        batch.drop_column("code")
