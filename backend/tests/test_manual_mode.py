"""Manual mode for POST /api/events/synthetic: explicit test-data entry.

The synthetic endpoint persists the payment, the event and the recovery case
(DETECTED, the first valid non-terminal actionable state of the existing state
machine) and STOPS: no creation-time escalation, no scheduled agent job, no
autonomous processing. Every pipeline decision is deferred to the explicit
manual workflow endpoints (agent/run -> action/execute -> outcome -> verify).
The autonomous pipeline (webhooks, replay, demo API, evaluation, internal
callers) must keep its complete behavior.
"""
from decimal import Decimal

from models import (
    BackgroundJob,
    OrderStatus,
    Payment,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.jobs import run_due_jobs

MANUAL_PAYLOAD = {
    "payment_id": "pay_manual_test_001",
    "event": "payment.failed",
    "amount_paise": 25500,
    "method": "CARD",
    "order_id": "order_manual_test_001",
    "error_code": "AUTHENTICATION_FAILED",
    "error_description": "Authentication failed during payment",
}


def _refresh(db, case_id):
    db.expire_all()
    return db.query(RecoveryCase).filter(RecoveryCase.id == case_id).one()


def _agent_jobs(db, case_id):
    return [
        job
        for job in db.query(BackgroundJob).filter(BackgroundJob.name == "run_agent").all()
        if (job.params or {}).get("case_id") == case_id
    ]


def test_manual_synthetic_stops_after_creation_with_unknown_order(client, db):
    """The exact manual verification payload: case created DETECTED and parked,
    even though the order reference does not resolve (deferred, not rejected,
    not escalated)."""
    response = client.post("/api/events/synthetic", json=MANUAL_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["processing_status"] == "PROCESSED"
    risk = body["risk_evaluation"]
    assert risk["decision"] == "CASE_CREATED"
    assert risk["mode"] == "manual"
    assert risk["deferred_ambiguity"] == "AMBIGUOUS_MISSING_ORDER"
    case_id = risk["case_id"]

    case = _refresh(db, case_id)
    assert case.status == RecoveryCaseStatus.DETECTED
    assert case.status not in (
        RecoveryCaseStatus.ESCALATED,
        RecoveryCaseStatus.RECOVERED,
        RecoveryCaseStatus.NOT_RECOVERED,
    )
    assert case.attempt_count == 0
    assert case.selected_action is None
    assert case.recovered_payment_id is None

    payment = db.query(Payment).filter(Payment.payment_id == "pay_manual_test_001").one()
    assert payment.status == PaymentStatus.FAILED
    assert payment.order_id is None
    assert payment.gateway_metadata["razorpay_order_id"] == "order_manual_test_001"

    assert _agent_jobs(db, case_id) == []

    run_due_jobs(db, force=True)
    assert _refresh(db, case_id).status == RecoveryCaseStatus.DETECTED

    execute = client.post(f"/api/cases/{case_id}/action/execute")
    assert execute.status_code == 200
    assert execute.json()["decision"] == "NOOP"
    assert "SAFETY_CHECK" in execute.json()["reason"]


def test_manual_clean_payload_full_manual_workflow_to_recovered(
    client, db, make_customer, make_order
):
    """Manual mode with a resolvable order: the full workflow is driven
    endpoint by endpoint with zero automatic processing in between."""
    customer = make_customer(lifetime_payments=20, lifetime_successes=19)
    order = make_order(customer=customer, amount=Decimal("255.00"))

    response = client.post(
        "/api/events/synthetic",
        json={
            "payment_id": "pay_manual_flow_001",
            "event": "payment.failed",
            "amount_paise": 25500,
            "method": "CARD",
            "order_id": order.order_id,
            "error_code": "AUTHENTICATION_FAILED",
            "error_description": "Authentication failed during payment",
        },
    )
    body = response.json()
    assert body["risk_evaluation"]["decision"] == "CASE_CREATED"
    assert body["risk_evaluation"]["deferred_ambiguity"] is None
    case_id = body["risk_evaluation"]["case_id"]

    assert _refresh(db, case_id).status == RecoveryCaseStatus.DETECTED
    assert _agent_jobs(db, case_id) == []
    run_due_jobs(db, force=True)
    assert _refresh(db, case_id).status == RecoveryCaseStatus.DETECTED

    agent = client.post(f"/api/cases/{case_id}/agent/run")
    assert agent.status_code == 200
    assert agent.json()["decision"] == "SAFETY_CHECK"

    execute = client.post(f"/api/cases/{case_id}/action/execute")
    assert execute.status_code == 200
    assert execute.json()["decision"] == "WAITING_FOR_RESULT"
    assert execute.json()["execution"]["recovery_payment_id"] == f"pay_rec_{case_id}_1"

    outcome = client.post(f"/api/cases/{case_id}/outcome", json={"outcome": "SUCCESS"})
    assert outcome.status_code == 200

    verify = client.post(f"/api/cases/{case_id}/verify")
    assert verify.status_code == 200
    assert verify.json()["decision"] == "RECOVERED"

    case = _refresh(db, case_id)
    assert case.status == RecoveryCaseStatus.RECOVERED
    assert case.recovered_payment_id == f"pay_rec_{case_id}_1"
    assert case.recovered_amount == Decimal("255.00")
    assert order.status == OrderStatus.PAID


def test_manual_mode_does_not_escalate_existing_active_case(
    client, db, make_customer, make_order, make_payment, post_event
):
    """Manual mode never mutates other cases either: the repeated-uncertain
    escalation of an active case is deferred, while the autonomous path keeps
    escalating it."""
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("1200.00"))
    p1 = make_payment(order=order, amount=Decimal("1200.00"), status=PaymentStatus.PENDING)

    first = post_event({"payment_id": p1.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"})
    active_case_id = first["risk_evaluation"]["case_id"]
    assert first["risk_evaluation"]["decision"] == "CASE_CREATED"

    make_payment(order=order, amount=Decimal("1200.00"), status=PaymentStatus.FAILED, failure_reason="NETWORK_ERROR")
    make_payment(order=order, amount=Decimal("1200.00"), status=PaymentStatus.FAILED, failure_reason="BANK_TIMEOUT")

    manual = client.post(
        "/api/events/synthetic",
        json={
            "payment_id": "pay_manual_uncertain_001",
            "event": "payment.failed",
            "amount_paise": 120000,
            "method": "upi",
            "order_id": order.order_id,
            "error_description": "Network connectivity issue",
        },
    )
    risk = manual.json()["risk_evaluation"]
    assert risk["decision"] == "DUPLICATE_ACTIVE_CASE"
    assert risk["deferred_ambiguity"] == "AMBIGUOUS_REPEATED_UNCERTAIN"
    assert _refresh(db, active_case_id).status != RecoveryCaseStatus.ESCALATED

    autonomous = post_event(
        {
            "payment_id": "pay_auto_uncertain_001",
            "event": "payment.failed",
            "amount_paise": 120000,
            "method": "upi",
            "order_id": order.order_id,
            "error_description": "Network connectivity issue",
        }
    )
    assert autonomous["risk_evaluation"]["decision"] == "CASE_ESCALATED"
    assert autonomous["risk_evaluation"]["reason"] == "AMBIGUOUS_REPEATED_UNCERTAIN"
    assert _refresh(db, active_case_id).status == RecoveryCaseStatus.ESCALATED


def test_replay_endpoint_keeps_autonomous_behavior(client, db, make_customer, make_order):
    """/api/events/replay is NOT manual: unknown order references are still
    rejected and clean events still schedule the background agent job."""
    rejected = client.post(
        "/api/events/replay",
        json={
            "payment_id": "pay_replay_unknown_001",
            "event": "payment.failed",
            "amount_paise": 25500,
            "method": "CARD",
            "order_id": "order_does_not_exist",
            "error_description": "Authentication failed",
        },
    )
    assert rejected.json()["processing_status"] == "REJECTED"
    assert rejected.json()["reason"] == "UNKNOWN_ORDER_REFERENCE"

    customer = make_customer(lifetime_payments=20, lifetime_successes=19)
    order = make_order(customer=customer, amount=Decimal("500.00"))
    created = client.post(
        "/api/events/replay",
        json={
            "payment_id": "pay_replay_clean_001",
            "event": "payment.failed",
            "amount_paise": 50000,
            "method": "UPI",
            "order_id": order.order_id,
            "error_description": "Insufficient funds",
        },
    )
    risk = created.json()["risk_evaluation"]
    assert risk["decision"] == "CASE_CREATED"
    assert "mode" not in risk
    case_id = risk["case_id"]
    assert _agent_jobs(db, case_id) != []
