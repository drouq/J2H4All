"""Phase 3 goal & plan tables (PRD §6.3/§8/§9/§11): goal, secondary_race,
macro_plan, session, session_result, proposal.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-06

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

JsonCol = JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "goal",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("format", sa.String(32), nullable=False),
        sa.Column("loop_km", sa.Float(), nullable=True),
        sa.Column("target_laps", sa.Integer(), nullable=True),
        sa.Column("race_date", sa.Date(), nullable=False),
        sa.Column("race_timezone", sa.String(64), nullable=True),
        sa.Column("floor_note", sa.Text(), nullable=True),
        sa.Column("stretch_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "secondary_race",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("type", sa.String(32), nullable=True),
        sa.Column("priority", sa.String(4), nullable=False, server_default="B"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "macro_plan",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("goal_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("b_race_approach", sa.Text(), nullable=True),
        sa.Column("phases", JsonCol, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "session",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("macro_plan_id", sa.Integer(), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=True),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("target_zone", sa.String(32), nullable=True),
        sa.Column("target_pace", sa.String(32), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("fueling_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="planned"),
        sa.Column("calendar_event_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_session_date", "session", ["date"])
    op.create_table(
        "session_result",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("activity_id", sa.BigInteger(), nullable=True),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("actual_distance_km", sa.Float(), nullable=True),
        sa.Column("actual_duration_min", sa.Float(), nullable=True),
        sa.Column("actual_avg_hr", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_session_result_session_id", "session_result", ["session_id"])
    op.create_index("ix_session_result_activity_id", "session_result", ["activity_id"])
    op.create_table(
        "proposal",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", JsonCol, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_proposal_status", "proposal", ["status"])


def downgrade() -> None:
    op.drop_table("proposal")
    op.drop_table("session_result")
    op.drop_table("session")
    op.drop_table("macro_plan")
    op.drop_table("secondary_race")
    op.drop_table("goal")
