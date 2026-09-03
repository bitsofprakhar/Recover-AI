from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .enums import RecoveryCaseStatus
from .payments import Payment


class RecoveryCase(Base):
    __tablename__ = "recovery_cases"
    __table_args__ = (
        Index(
            "uq_recovery_cases_active_per_payment",
            "payment_id",
            unique=True,
            postgresql_where=text("status NOT IN ('RECOVERED', 'NOT_RECOVERED', 'STOPPED', 'ESCALATED')"),
        ),
        Index("uq_recovery_cases_recovered_payment", "recovered_payment_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("payments.id"), nullable=False, index=True)
    revenue_at_risk: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[RecoveryCaseStatus] = mapped_column(Enum(RecoveryCaseStatus, name="recovery_case_status"), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recovered_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recovered_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    payment: Mapped[Payment] = relationship(back_populates="recovery_cases")
    agent_actions: Mapped[list["AgentAction"]] = relationship(back_populates="case")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="case")
