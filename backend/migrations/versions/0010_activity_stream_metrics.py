"""Durability rollup from per-second activity streams (decoupling / HR drift / pace CV).

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-07

"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activity", sa.Column("stream_metrics", sa.JSON(), nullable=True))
    op.add_column(
        "activity",
        sa.Column("streams_synced", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("activity", "streams_synced")
    op.drop_column("activity", "stream_metrics")
