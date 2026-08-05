"""Goal: the fields every race format other than a backyard needs.

`loop_km` and `target_laps` describe a backyard and mean nothing to a marathon.
Adding distance, climbing and a target time lets `Goal.format` select doctrine
from coach/formats/ with the goal row actually carrying that format's facts.

All three are nullable and additive, so this is safe to pre-apply and old code
ignores them.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-05

"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("goal", sa.Column("distance_km", sa.Float(), nullable=True))
    op.add_column("goal", sa.Column("elevation_gain_m", sa.Float(), nullable=True))
    # Free text, not seconds: it only feeds a prompt, and "sub-3:30" states a goal
    # more honestly than a rounded integer would.
    op.add_column("goal", sa.Column("target_time", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("goal", "target_time")
    op.drop_column("goal", "elevation_gain_m")
    op.drop_column("goal", "distance_km")
