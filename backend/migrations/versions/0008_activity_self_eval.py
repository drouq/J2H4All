"""Capture the athlete's per-activity self-evaluation (feel + RPE) from Garmin's
detail endpoint (summaryDTO.directWorkoutFeel / directWorkoutRpe).

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activity", sa.Column("feel", sa.Integer(), nullable=True))
    op.add_column("activity", sa.Column("rpe", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("activity", "rpe")
    op.drop_column("activity", "feel")
