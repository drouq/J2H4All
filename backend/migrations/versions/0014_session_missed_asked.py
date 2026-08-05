"""Session: when the coach raised a planned run that never happened.

Raised at most ONCE per session — this column is what guarantees it. See
coach/missed.py.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-05

"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session",
                  sa.Column("missed_asked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("session", "missed_asked_at")
