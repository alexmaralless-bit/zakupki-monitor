"""product: ai_cache, crm_exports, settings, поля карточки и расписания

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-04 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("profiles") as batch:
        batch.add_column(sa.Column("interval_minutes", sa.Integer(), nullable=True))
    with op.batch_alter_table("tenders") as batch:
        batch.add_column(sa.Column("deadline_time", sa.Time(), nullable=True))
        batch.add_column(sa.Column("note", sa.Text(), nullable=True))
    op.create_table(
        "ai_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reg_number", sa.String(length=32), nullable=False),
        sa.Column("criterion_hash", sa.String(length=64), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reg_number", "criterion_hash", "text_hash"),
    )
    op.create_index("ix_ai_cache_reg_number", "ai_cache", ["reg_number"])
    op.create_table(
        "crm_exports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tender_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("crm_exports")
    op.drop_index("ix_ai_cache_reg_number", table_name="ai_cache")
    op.drop_table("ai_cache")
    with op.batch_alter_table("tenders") as batch:
        batch.drop_column("note")
        batch.drop_column("deadline_time")
    with op.batch_alter_table("profiles") as batch:
        batch.drop_column("interval_minutes")
