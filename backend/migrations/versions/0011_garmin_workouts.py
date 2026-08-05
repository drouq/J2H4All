"""Garmin workout push: coach-prescribed structure + pushed workout/schedule ids.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-10

"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session", sa.Column("structure", sa.JSON(), nullable=True))
    op.add_column("session", sa.Column("garmin_workout_id", sa.String(64), nullable=True))
    op.add_column("session", sa.Column("garmin_schedule_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("session", "garmin_schedule_id")
    op.drop_column("session", "garmin_workout_id")
    op.drop_column("session", "structure")
