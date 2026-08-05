"""Garmin ingest tables: activity, wellness_daily, fitness_marker, sync_run.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-05

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JsonCol = JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "activity",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("start_time_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_time_local", sa.DateTime(), nullable=True),
        sa.Column("activity_type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("elevation_gain_m", sa.Float(), nullable=True),
        sa.Column("avg_hr", sa.Integer(), nullable=True),
        sa.Column("max_hr", sa.Integer(), nullable=True),
        sa.Column("avg_speed_mps", sa.Float(), nullable=True),
        sa.Column("avg_run_cadence", sa.Float(), nullable=True),
        sa.Column("calories", sa.Float(), nullable=True),
        sa.Column("aerobic_te", sa.Float(), nullable=True),
        sa.Column("anaerobic_te", sa.Float(), nullable=True),
        sa.Column("vo2max", sa.Float(), nullable=True),
        sa.Column("hr_zones", JsonCol, nullable=True),
        sa.Column("laps", JsonCol, nullable=True),
        sa.Column("raw", JsonCol, nullable=False),
        sa.Column("detail_synced", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_activity_activity_type", "activity", ["activity_type"])
    op.create_index("ix_activity_start_time_utc", "activity", ["start_time_utc"])

    op.create_table(
        "wellness_daily",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("resting_hr", sa.Integer(), nullable=True),
        sa.Column("hrv_last_night_avg", sa.Integer(), nullable=True),
        sa.Column("hrv_status", sa.String(32), nullable=True),
        sa.Column("sleep_seconds", sa.Integer(), nullable=True),
        sa.Column("sleep_score", sa.Integer(), nullable=True),
        sa.Column("sleep_stages", JsonCol, nullable=True),
        sa.Column("body_battery_high", sa.Integer(), nullable=True),
        sa.Column("body_battery_low", sa.Integer(), nullable=True),
        sa.Column("stress_avg", sa.Integer(), nullable=True),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("weight_kg", sa.Float(), nullable=True),
        sa.Column("body_fat_pct", sa.Float(), nullable=True),
        sa.Column("raw", JsonCol, nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "fitness_marker",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("value", JsonCol, nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("date", "kind", name="uq_fitness_marker_date_kind"),
    )
    op.create_index("ix_fitness_marker_date", "fitness_marker", ["date"])

    op.create_table(
        "sync_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("stats", JsonCol, nullable=True),
        sa.Column("alerted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_sync_run_status", "sync_run", ["status"])


def downgrade() -> None:
    op.drop_table("sync_run")
    op.drop_table("fitness_marker")
    op.drop_table("wellness_daily")
    op.drop_table("activity")
