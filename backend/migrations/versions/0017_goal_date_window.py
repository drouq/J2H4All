"""Goal: a race date that isn't decided yet.

Athletes often know roughly when the race is before they know the day — waiting on
a ballot, choosing between two events, or organising it themselves with friends.
Until now the only option was to pick a date and pretend. `date_window_start` holds
the EARLIEST candidate while the date is open; `race_date` stays the planning anchor
(the latest candidate), so every existing countdown keeps working. NULL means the
date is fixed.

Nullable and additive, so this is safe to pre-apply and old code ignores it.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("goal", sa.Column("date_window_start", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("goal", "date_window_start")
