"""Session result: why a session came in off plan (asked, not assumed).

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-03

"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session_result",
                  sa.Column("deviation_asked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("session_result",
                  sa.Column("deviation_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("session_result", "deviation_reason")
    op.drop_column("session_result", "deviation_asked_at")
