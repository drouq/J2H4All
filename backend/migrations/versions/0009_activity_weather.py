"""Weather at each run's start (Open-Meteo) — context for interpreting a run.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activity", sa.Column("weather_temp_c", sa.Float(), nullable=True))
    op.add_column("activity", sa.Column("weather_humidity", sa.Integer(), nullable=True))
    op.add_column("activity", sa.Column("weather_feels_c", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("activity", "weather_feels_c")
    op.drop_column("activity", "weather_humidity")
    op.drop_column("activity", "weather_temp_c")
