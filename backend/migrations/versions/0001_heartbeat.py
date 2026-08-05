"""Phase 0 heartbeat table — proves migrations + DB round trip. Dropped in a later phase.

Revision ID: 0001
Revises:
Create Date: 2026-07-05

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "heartbeat",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("heartbeat")
