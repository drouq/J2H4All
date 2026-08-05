"""Phase 5: adaptation & proactivity.
- checkin: daily subjective check-in
- session_result.read_summary/flagged: coach's planned-vs-actual read
- scheduled_job_run: local-clock dispatch idempotency
- proposal.origin: where a proposal came from (web / weekly_review / red_flag)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-06

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

JsonCol = JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("proposal", sa.Column("origin", sa.String(24), nullable=False, server_default="web"))

    op.add_column("session_result", sa.Column("read_summary", sa.Text(), nullable=True))
    op.add_column("session_result", sa.Column("flagged", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    op.create_table(
        "checkin",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False, unique=True),
        sa.Column("energy", sa.Integer(), nullable=True),
        sa.Column("soreness", sa.Integer(), nullable=True),
        sa.Column("motivation", sa.Integer(), nullable=True),
        sa.Column("life_stress", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("raw", JsonCol, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "scheduled_job_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job", sa.String(32), nullable=False),
        sa.Column("ran_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job", "ran_on", name="uq_scheduled_job_run"),
    )


def downgrade() -> None:
    op.drop_table("scheduled_job_run")
    op.drop_table("checkin")
    op.drop_column("session_result", "flagged")
    op.drop_column("session_result", "read_summary")
    op.drop_column("proposal", "origin")
