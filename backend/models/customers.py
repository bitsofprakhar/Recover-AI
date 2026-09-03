from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)

    lifetime_payments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lifetime_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prior_recovery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prior_recovery_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")
