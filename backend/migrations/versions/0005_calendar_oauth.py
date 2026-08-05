"""Phase 4 (PRD §10): Google Calendar integration.
- oauth_credential: server-side refresh token store (Google Calendar).
- user_state.training_calendar_id: the dedicated J2H4All calendar we own.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-06

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_credential",
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("user_state", sa.Column("training_calendar_id", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("user_state", "training_calendar_id")
    op.drop_table("oauth_credential")
