"""Structured case context assembly for the agent (Phase 5).

The backend - not the LLM - assembles and validates the diagnostic context.
The three context read tools are executed through the controlled tool layer so
every context lookup is logged, then a deterministic assessment applies the
ambiguity triggers of README Section 9 to the current data. The LLM receives
the assembled context (with customer PII masked) and cannot skip this step.
"""
from sqlalchemy.orm import Session

from config import settings
from models import OrderStatus, Payment, PaymentStatus, RecoveryCase
from services.revenue_risk import UNCERTAIN_FAILURE_REASONS

from .tools import execute_tool


def build_context(db: Session, case: RecoveryCase) -> tuple[dict, list[dict]]:
    payment_out = execute_tool(db, case, "get_payment_status", {})
    order_out = execute_tool(db, case, "get_order_details", {})
    customer_out = execute_tool(db, case, "get_customer_history", {})
    calls = [{"tool_name": out["tool"], "status": out["status"]} for out in (payment_out, order_out, customer_out)]

    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    order = payment.order

    prior_cases = []
    if order is not None:
        rows = (
            db.query(RecoveryCase)
            .join(Payment, RecoveryCase.payment_id == Payment.id)
            .filter(Payment.order_id == order.id, RecoveryCase.id != case.id)
            .order_by(RecoveryCase.id)
            .all()
        )
        prior_cases = [
            {
                "case_id": row.id,
                "status": row.status.value,
                "revenue_at_risk": str(row.revenue_at_risk),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    context = {
        "case": {
            "case_id": case.id,
            "status": case.status.value,
            "revenue_at_risk": str(case.revenue_at_risk),
            "attempt_count": case.attempt_count,
            "expiry": case.expiry.isoformat(),
            "created_at": case.created_at.isoformat(),
        },
        "payment": payment_out,
        "order": order_out,
        "customer": customer_out,
        "prior_attempts": {
            "payments_on_order": customer_out.get("order_payment_history", []) if customer_out.get("found") else [],
            "prior_recovery_cases_on_order": prior_cases,
        },
    }
    context["assessment"] = _assess(db, payment, order)
    return context, calls


def _assess(db: Session, payment: Payment, order) -> dict:
    ambiguities: list[str] = []
    if order is None:
        ambiguities.append("MISSING_ORDER_IDENTITY")
    else:
        if payment.status != PaymentStatus.FAILED:
            ambiguities.append("PAYMENT_STATE_CHANGED")
        if order.status == OrderStatus.PAID:
            ambiguities.append("CONFLICTING_ORDER_STATE")
        if payment.amount != order.amount:
            ambiguities.append("AMOUNT_MISMATCH")
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
            ambiguities.append("REPEATED_UNCERTAIN_FAILURES")
    return {"complete": not ambiguities, "ambiguities": ambiguities}
