"""Phase 6 (PRD §6.4): coaching chat history shared by both surfaces.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("surface", sa.String(16), nullable=False, server_default="web"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_message_created_at", "message", ["created_at"])


def downgrade() -> None:
    op.drop_table("message")
