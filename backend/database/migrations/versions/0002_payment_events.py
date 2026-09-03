"""payment_events: raw event store for the intake pipeline

Revision ID: 0002_payment_events
Revises: 0001_initial
Create Date: 2026-08-31

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_payment_events"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payment_ref", sa.String(length=64), nullable=False),
        sa.Column("entity_status", sa.String(length=32), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_payment_events_event_id"), "payment_events", ["event_id"], unique=True)
    op.create_index(op.f("ix_payment_events_payment_ref"), "payment_events", ["payment_ref"], unique=False)
    op.create_index(op.f("ix_payment_events_payment_id"), "payment_events", ["payment_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_events_payment_id"), table_name="payment_events")
    op.drop_index(op.f("ix_payment_events_payment_ref"), table_name="payment_events")
    op.drop_index(op.f("ix_payment_events_event_id"), table_name="payment_events")
    op.drop_table("payment_events")
