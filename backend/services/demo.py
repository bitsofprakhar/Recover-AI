"""Deterministic demo-case selection (post-Phase-12 reliability fix).

The demo previously relied on fixed seed payment ids (pay_0077, ...). After a
demo run completes, that payment's order is PAID and its case terminal, so a
second demo without a database reset produces born-escalated cases and
/action/execute correctly NOOPs.

This module selects, from the CURRENT database state, the next failed payment
that is guaranteed to produce a fresh, clean recovery case through the
existing pipeline:

- the payment is FAILED and its failure reason is not an uncertain reason
  (and the order has fewer uncertain failures than the escalation threshold);
- the order exists, is not PAID, and its authoritative amount matches;
- no recovery case exists for any payment on that order yet (fresh pool
  consumption - every demo call advances to a new payment).

Selection only picks an input the existing rules already accept: it changes no
validation, no state machine rule and no safety behaviour. Ordering is
deterministic (payment id).
"""
from sqlalchemy.orm import Session

from config import settings
from models import Order, OrderStatus, Payment, PaymentStatus, RecoveryCase
from services.revenue_risk import UNCERTAIN_FAILURE_REASONS


def demo_eligible_payments(db: Session) -> list[Payment]:
    """All failed payments that would create a fresh clean case right now, in deterministic order."""
    payments = db.query(Payment).filter(Payment.status == PaymentStatus.FAILED).order_by(Payment.id).all()
    eligible: list[Payment] = []
    for payment in payments:
        order = db.query(Order).filter(Order.id == payment.order_id).one_or_none()
        if order is None:
            continue
        if order.status == OrderStatus.PAID:
            continue
        if payment.amount != order.amount:
            continue
        if payment.failure_reason in UNCERTAIN_FAILURE_REASONS:
            continue
        uncertain = (
            db.query(Payment)
            .filter(
                Payment.order_id == order.id,
                Payment.status == PaymentStatus.FAILED,
                Payment.failure_reason.in_(UNCERTAIN_FAILURE_REASONS),
            )
            .count()
        )
        if uncertain >= settings.repeated_uncertain_failure_threshold:
            continue
        prior_cases = (
            db.query(RecoveryCase)
            .join(Payment, RecoveryCase.payment_id == Payment.id)
            .filter(Payment.order_id == order.id)
            .count()
        )
        if prior_cases > 0:
            continue
        eligible.append(payment)
    return eligible
