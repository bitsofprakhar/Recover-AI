"""initial schema: customers, orders, payments, recovery_cases, agent_actions, audit_logs

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-31

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

ORDER_STATUSES = ("CREATED", "PAID", "ATTEMPTED")
PAYMENT_STATUSES = ("CREATED", "PENDING", "CAPTURED", "FAILED")
CASE_STATUSES = (
    "DETECTED",
    "DIAGNOSING",
    "SCORED",
    "ACTION_SELECTED",
    "SAFETY_CHECK",
    "ACTION_EXECUTED",
    "WAITING_FOR_RESULT",
    "RECOVERED",
    "NOT_RECOVERED",
    "STOPPED",
    "ESCALATED",
)
ACTIVE_CASE_WHERE = "status NOT IN ('RECOVERED', 'NOT_RECOVERED', 'STOPPED', 'ESCALATED')"


def upgrade() -> None:
    order_status = postgresql.ENUM(*ORDER_STATUSES, name="order_status", create_type=False)
    payment_status = postgresql.ENUM(*PAYMENT_STATUSES, name="payment_status", create_type=False)
    recovery_case_status = postgresql.ENUM(*CASE_STATUSES, name="recovery_case_status", create_type=False)
    order_status.create(op.get_bind(), checkfirst=True)
    payment_status.create(op.get_bind(), checkfirst=True)
    recovery_case_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "customers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("lifetime_payments", sa.Integer(), nullable=False),
        sa.Column("lifetime_successes", sa.Integer(), nullable=False),
        sa.Column("prior_recovery_attempts", sa.Integer(), nullable=False),
        sa.Column("prior_recovery_successes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_customers_customer_id"), "customers", ["customer_id"], unique=True)

    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", order_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_orders_customer_id"), "orders", ["customer_id"], unique=False)
    op.create_index(op.f("ix_orders_order_id"), "orders", ["order_id"], unique=True)

    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("payment_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("status", payment_status, nullable=False),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column("gateway_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_payments_order_id"), "payments", ["order_id"], unique=False)
    op.create_index(op.f("ix_payments_payment_id"), "payments", ["payment_id"], unique=True)
    op.create_index(op.f("ix_payments_status"), "payments", ["status"], unique=False)

    op.create_table(
        "recovery_cases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("payment_id", sa.BigInteger(), nullable=False),
        sa.Column("revenue_at_risk", sa.Numeric(12, 2), nullable=False),
        sa.Column("diagnosis", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("selected_action", sa.String(length=64), nullable=True),
        sa.Column("status", recovery_case_status, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("expiry", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_recovery_cases_payment_id"), "recovery_cases", ["payment_id"], unique=False)
    op.create_index(op.f("ix_recovery_cases_status"), "recovery_cases", ["status"], unique=False)
    op.create_index(
        "uq_recovery_cases_active_per_payment",
        "recovery_cases",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_CASE_WHERE),
    )

    op.create_table(
        "agent_actions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("allowed", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["recovery_cases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_actions_case_id"), "agent_actions", ["case_id"], unique=False)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.BigInteger(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["recovery_cases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_logs_case_id"), "audit_logs", ["case_id"], unique=False)

    op.execute(
        """
        CREATE OR REPLACE FUNCTION recoverai_audit_logs_append_only()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_immutable
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION recoverai_audit_logs_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_immutable ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS recoverai_audit_logs_append_only()")
    op.drop_index(op.f("ix_audit_logs_case_id"), table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index(op.f("ix_agent_actions_case_id"), table_name="agent_actions")
    op.drop_table("agent_actions")
    op.drop_index("uq_recovery_cases_active_per_payment", table_name="recovery_cases")
    op.drop_index(op.f("ix_recovery_cases_status"), table_name="recovery_cases")
    op.drop_index(op.f("ix_recovery_cases_payment_id"), table_name="recovery_cases")
    op.drop_table("recovery_cases")
    op.drop_index(op.f("ix_payments_status"), table_name="payments")
    op.drop_index(op.f("ix_payments_payment_id"), table_name="payments")
    op.drop_index(op.f("ix_payments_order_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_index(op.f("ix_orders_order_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_customer_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_index(op.f("ix_customers_customer_id"), table_name="customers")
    op.drop_table("customers")
    sa.Enum(name="recovery_case_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="payment_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="order_status").drop(op.get_bind(), checkfirst=True)
