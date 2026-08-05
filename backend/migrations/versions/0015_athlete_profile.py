"""Athlete profile: who the athlete is, as data rather than as prompt text.

The coaching doctrine used to carry one person's name, age, pronouns and
physiology as hardcoded prose. This table is where those facts live now, so a
fresh install coaches whoever owns it. See coach/doctrine.py.

Also drops the dietary_profile default of one athlete's diet. Existing rows are
left alone — only the DEFAULT for new rows changes, so nobody's recorded diet is
rewritten by this migration.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-05

"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "athlete_profile",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=True),
        sa.Column("pronouns", sa.String(length=32), nullable=False,
                  server_default=sa.text("'they/them'")),
        sa.Column("birthdate", sa.Date(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("data_caveats", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Batch mode so this also applies on SQLite, which cannot ALTER a column in
    # place. Postgres ignores the batch wrapper and does a plain ALTER.
    with op.batch_alter_table("dietary_profile") as batch:
        batch.alter_column("diet", existing_type=sa.String(length=64),
                           existing_nullable=False,
                           server_default=sa.text("'unspecified'"))


def downgrade() -> None:
    with op.batch_alter_table("dietary_profile") as batch:
        batch.alter_column("diet", existing_type=sa.String(length=64),
                           existing_nullable=False,
                           server_default=sa.text("'vegetarian'"))
    op.drop_table("athlete_profile")
