"""attribution: verified recovery columns on recovery_cases (Phase 9)

Revision ID: 0003_attribution
Revises: 0002_payment_events
Create Date: 2026-09-02

"""
from alembic import op
import sqlalchemy as sa

revision = "0003_attribution"
down_revision = "0002_payment_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recovery_cases", sa.Column("recovered_payment_id", sa.String(length=64), nullable=True))
    op.add_column("recovery_cases", sa.Column("recovered_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("recovery_cases", sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_recovery_cases_recovered_payment",
        "recovery_cases",
        ["recovered_payment_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_recovery_cases_recovered_payment", table_name="recovery_cases")
    op.drop_column("recovery_cases", "recovered_at")
    op.drop_column("recovery_cases", "recovered_amount")
    op.drop_column("recovery_cases", "recovered_payment_id")
