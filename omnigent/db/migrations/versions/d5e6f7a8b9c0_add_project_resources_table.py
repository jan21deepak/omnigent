"""add project_resources table

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-03

Lets a project hold non-agent artifacts — links, repositories, local services
and notes — alongside its member sessions, so one project is the whole
workspace for a piece of work rather than a list of conversations.

Additive. There are no foreign-key constraints (schema Rule R032): the
project relationship is enforced by the application, not the database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``project_resources`` table."""
    op.create_table(
        "project_resources",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # UUID PKs stored as 16 raw bytes (Uuid16), read back as bare hex.
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("uri", sa.String(2048), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    # created_at is in the key so "list a project's resources" (WHERE
    # workspace_id, project_id ORDER BY created_at, id) is a pure index scan.
    op.create_index(
        "ix_project_resources_project_id",
        "project_resources",
        ["workspace_id", "project_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``project_resources`` table."""
    op.drop_index("ix_project_resources_project_id", table_name="project_resources")
    op.drop_table("project_resources")
