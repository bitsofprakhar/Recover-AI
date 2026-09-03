"""Deterministic demo-flow tests: the exact synthetic event shapes the demo uses.

1. A failure event for a new payment id that explicitly references an order
   unknown to the merchant data is REJECTED (UNKNOWN_ORDER_REFERENCE): the
   reference contradicts our records and must never be silently dropped into
   an orderless payment that escalates as AMBIGUOUS_MISSING_ORDER.
2. A failure event with a resolvable order reference and matching amount on a
   recoverable profile runs the full executable path - intake, case creation,
   agent diagnosis, scoring, safety gate, action execution, outcome,
   verification - and ends RECOVERED with attributed revenue.
3. A failure event with no order reference at all is the genuine
   missing-identity ambiguity: born ESCALATED with zero attempts, no agent
   job, and no recovery action ever executed.
"""
from decimal import Decimal

from models import (
    AuditLog,
    BackgroundJob,
    Payment,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.action_executor import execute_action
from services.jobs import run_due_jobs
from services.outcome_simulator import simulate_outcome
from services.verification import verify_outcome

SUCCESS_EVENT = {
    "payment_id": "pay_demo_success_001",
    "event": "payment.failed",
    "amount_paise": 150000,
    "method": "card",
    "error_code": "AUTHENTICATION_FAILED",
    "error_description": "Authentication failed during payment",
}


def _refresh(db, case):
    db.expire_all()
    return db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()


def _audits(db, case):
    return db.query(AuditLog).filter(AuditLog.case_id == case.id).order_by(AuditLog.id).all()


def test_unknown_order_reference_event_is_rejected(db, make_customer, make_order, post_event):
    customer = make_customer()
    make_order(customer=customer, amount=Decimal("1500.00"))

    result = post_event(dict(SUCCESS_EVENT, order_id="order_success_001"))

    assert result["processing_status"] == "REJECTED"
    assert result["reason"] == "UNKNOWN_ORDER_REFERENCE"
    assert result["payment_status"] is None
    assert db.query(Payment).filter(Payment.payment_id == "pay_demo_success_001").one_or_none() is None
    assert db.query(RecoveryCase).count() == 0
    assert db.query(AuditLog).filter(AuditLog.event_type.like("risk.%")).count() == 0


def test_deterministic_success_event_full_path_recovered(
    db, make_customer, make_order, make_payment, post_event
):
    customer = make_customer(lifetime_payments=20, lifetime_successes=19)
    order = make_order(customer=customer, amount=Decimal("1500.00"))

    result = post_event(dict(SUCCESS_EVENT, order_id=order.order_id))

    assert result["processing_status"] == "PROCESSED"
    risk = result["risk_evaluation"]
    assert risk["decision"] == "CASE_CREATED"
    assert risk["reason"] is None
    case_id = risk["case_id"]

    payment = db.query(Payment).filter(Payment.payment_id == "pay_demo_success_001").one()
    assert payment.order_id == order.id

    run_due_jobs(db)
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).one()
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    assert case.score == 82
    assert case.selected_action == "RETRY_PAYMENT_LINK"

    execution = execute_action(db, case)
    assert execution["decision"] == "WAITING_FOR_RESULT"
    assert execution["execution"]["recovery_payment_id"] == f"pay_rec_{case_id}_1"

    simulate_outcome(db, _refresh(db, case), "SUCCESS")
    verification = verify_outcome(db, _refresh(db, case))

    assert verification["decision"] == "RECOVERED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.RECOVERED
    assert case.recovered_payment_id == f"pay_rec_{case_id}_1"
    assert case.recovered_amount == Decimal("1500.00")
    assert case.attempt_count == 1
    events = [log.event_type for log in _audits(db, case)]
    assert events == [
        "risk.case_created",
        "agent.diagnosis_started",
        "agent.diagnosis_completed",
        "agent.scored",
        "agent.action_selected",
        "gate.submitted",
        "gate.allowed",
        "action.executed",
        "action.monitoring_started",
        "outcome.success",
        "verification.recovered",
    ]


def test_deterministic_missing_order_event_escalates(db, post_event):
    result = post_event(dict(SUCCESS_EVENT, payment_id="pay_demo_escalate_001"))

    assert result["processing_status"] == "PROCESSED"
    risk = result["risk_evaluation"]
    assert risk["decision"] == "CASE_ESCALATED"
    assert risk["reason"] == "AMBIGUOUS_MISSING_ORDER"

    case = db.query(RecoveryCase).one()
    assert case.status == RecoveryCaseStatus.ESCALATED
    assert case.attempt_count == 0
    assert case.selected_action is None
    assert case.recovered_payment_id is None

    payment = db.query(Payment).filter(Payment.payment_id == "pay_demo_escalate_001").one()
    assert payment.order_id is None

    assert db.query(BackgroundJob).filter(BackgroundJob.name == "run_agent").count() == 0
    assert db.query(Payment).filter(Payment.payment_id.like("pay_rec_%")).count() == 0
    events = [log.event_type for log in _audits(db, case)]
    assert events == ["risk.case_created", "risk.case_escalated"]
