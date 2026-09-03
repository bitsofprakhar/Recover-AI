"""Revenue-at-risk detection and deterministic recovery case creation (Phase 4).

Evaluated by the event pipeline after every processed payment event, on the
payment's current state:

- CAPTURED payments never create cases (already successful / reconciled).
- PENDING and CREATED payments never create cases: they are routed to
  verification first (verification-first rule, README Section 10).
- FAILED payments with revenue at risk create a recovery case in DETECTED,
  unless an active case already exists for the same payment or for any
  payment on the same order (idempotency at transaction/order level).
  Terminal cases do not block new cases: a new failure after a terminal
  case is new revenue at risk.
- Ambiguous failures escalate immediately (state machine rule 17):
  missing order identity, conflicting order/payment state, amount mismatch,
  or repeated uncertain failures. Ambiguous cases never reach recovery
  actions.

Every decision is audited and returned to the caller for the API response.

Processing modes (see services.event_intake for the full contract):

- mode="autonomous" (default): the complete Phase 4 behavior described above.
- mode="manual" (POST /api/events/synthetic only): persist the payment, the
  event and the recovery case in DETECTED, then STOP. Creation-time
  ambiguities are NOT escalated here - they are recorded on the case and
  deferred to the explicit agent run (POST /api/cases/{id}/agent/run), which
  applies the same ambiguity triggers through the diagnosis context
  assessment - and no background agent job is scheduled. Nothing else
  differs: the same state machine, the same audits, the same rules.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from config import settings
from models import (
    TERMINAL_CASE_STATUSES,
    Order,
    OrderStatus,
    Payment,
    PaymentEvent,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.audit import record

UNCERTAIN_FAILURE_REASONS = {"NETWORK_ERROR", "BANK_TIMEOUT"}

DECISION_CASE_CREATED = "CASE_CREATED"
DECISION_CASE_ESCALATED = "CASE_ESCALATED"
DECISION_DUPLICATE = "DUPLICATE_ACTIVE_CASE"
DECISION_PENDING_VERIFICATION = "PENDING_VERIFICATION"
DECISION_ALREADY_SUCCESSFUL = "ALREADY_SUCCESSFUL_IGNORED"


def _active_case_for_payment_or_order(db: Session, payment: Payment) -> RecoveryCase | None:
    conditions = [Payment.id == payment.id]
    if payment.order_id is not None:
        conditions.append(Payment.order_id == payment.order_id)
    return (
        db.query(RecoveryCase)
        .join(Payment, RecoveryCase.payment_id == Payment.id)
        .filter(RecoveryCase.status.notin_(list(TERMINAL_CASE_STATUSES)))
        .filter(or_(*conditions))
        .order_by(RecoveryCase.id.desc())
        .first()
    )


def _uncertain_failure_count(db: Session, payment: Payment) -> int:
    if payment.order_id is None:
        return 0
    count = (
        db.query(func.count(Payment.id))
        .filter(
            Payment.order_id == payment.order_id,
            Payment.status == PaymentStatus.FAILED,
            Payment.failure_reason.in_(UNCERTAIN_FAILURE_REASONS),
        )
        .scalar()
    )
    return count or 0


def _creation_ambiguity(db: Session, payment: Payment, order: Order | None) -> str | None:
    if order is None:
        return "AMBIGUOUS_MISSING_ORDER"
    if order.status == OrderStatus.PAID:
        return "AMBIGUOUS_CONFLICTING_STATE"
    if payment.amount != order.amount:
        return "AMBIGUOUS_AMOUNT_MISMATCH"
    if _uncertain_failure_count(db, payment) >= settings.repeated_uncertain_failure_threshold:
        return "AMBIGUOUS_REPEATED_UNCERTAIN"
    return None


def _escalate(db: Session, case: RecoveryCase, reason: str, trigger: dict) -> None:
    previous = case.status
    case.status = RecoveryCaseStatus.ESCALATED
    record(
        db,
        "risk.case_escalated",
        case_id=case.id,
        from_status=previous,
        to_status=RecoveryCaseStatus.ESCALATED,
        payload={**trigger, "reason": reason},
    )


def evaluate(db: Session, payment: Payment, trigger_event: PaymentEvent, mode: str = "autonomous") -> dict:
    trigger = {"trigger_event_id": trigger_event.event_id, "payment_id": payment.payment_id}

    if payment.status == PaymentStatus.CAPTURED:
        record(db, "risk.already_successful", payload={**trigger, "reason": "ALREADY_SUCCESSFUL"})
        return {
            "decision": DECISION_ALREADY_SUCCESSFUL,
            "case_id": None,
            "reason": None,
            "revenue_at_risk": None,
        }

    if payment.status in (PaymentStatus.PENDING, PaymentStatus.CREATED):
        record(db, "risk.pending_verification", payload={**trigger, "reason": "PENDING_VERIFICATION"})
        return {
            "decision": DECISION_PENDING_VERIFICATION,
            "case_id": None,
            "reason": None,
            "revenue_at_risk": None,
        }

    active = _active_case_for_payment_or_order(db, payment)
    if active is not None:
        if _uncertain_failure_count(db, payment) >= settings.repeated_uncertain_failure_threshold:
            if mode == "manual":
                record(
                    db,
                    "risk.case_duplicate",
                    case_id=active.id,
                    payload={
                        **trigger,
                        "existing_case_id": active.id,
                        "deferred_ambiguity": "AMBIGUOUS_REPEATED_UNCERTAIN",
                    },
                )
                return {
                    "decision": DECISION_DUPLICATE,
                    "case_id": active.id,
                    "reason": None,
                    "revenue_at_risk": None,
                    "mode": mode,
                    "deferred_ambiguity": "AMBIGUOUS_REPEATED_UNCERTAIN",
                    "note": "manual mode: the active case is left untouched; the ambiguity is deferred to explicit processing",
                }
            _escalate(db, active, "AMBIGUOUS_REPEATED_UNCERTAIN", trigger)
            return {
                "decision": DECISION_CASE_ESCALATED,
                "case_id": active.id,
                "reason": "AMBIGUOUS_REPEATED_UNCERTAIN",
                "revenue_at_risk": str(active.revenue_at_risk),
            }
        record(
            db,
            "risk.case_duplicate",
            case_id=active.id,
            payload={**trigger, "existing_case_id": active.id},
        )
        return {
            "decision": DECISION_DUPLICATE,
            "case_id": active.id,
            "reason": None,
            "revenue_at_risk": None,
        }

    order = payment.order
    revenue_at_risk = order.amount if order is not None else payment.amount
    ambiguity = _creation_ambiguity(db, payment, order)

    case = RecoveryCase(
        payment_id=payment.id,
        revenue_at_risk=revenue_at_risk,
        status=RecoveryCaseStatus.DETECTED,
        attempt_count=0,
        expiry=datetime.now(timezone.utc) + timedelta(hours=settings.case_window_hours),
    )
    db.add(case)
    db.flush()
    record(
        db,
        "risk.case_created",
        case_id=case.id,
        to_status=RecoveryCaseStatus.DETECTED,
        payload={
            **trigger,
            "order_id": order.order_id if order is not None else None,
            "revenue_at_risk": str(revenue_at_risk),
            "ambiguity_reason": ambiguity,
        },
    )

    if mode == "manual":
        return {
            "decision": DECISION_CASE_CREATED,
            "case_id": case.id,
            "reason": None,
            "revenue_at_risk": str(revenue_at_risk),
            "mode": mode,
            "deferred_ambiguity": ambiguity,
            "note": (
                "manual mode: the case stays in DETECTED with no agent job scheduled; drive it with "
                "POST /api/cases/{case_id}/agent/run"
                + (" (creation-time ambiguity is deferred to the diagnosis context assessment)" if ambiguity else "")
            ),
        }

    if ambiguity is not None:
        _escalate(db, case, ambiguity, trigger)
        return {
            "decision": DECISION_CASE_ESCALATED,
            "case_id": case.id,
            "reason": ambiguity,
            "revenue_at_risk": str(revenue_at_risk),
        }

    from services.jobs import agent_job_key, schedule_job

    schedule_job(db, "run_agent", {"case_id": case.id}, datetime.now(timezone.utc), agent_job_key(case.id, 0))

    return {
        "decision": DECISION_CASE_CREATED,
        "case_id": case.id,
        "reason": None,
        "revenue_at_risk": str(revenue_at_risk),
    }
