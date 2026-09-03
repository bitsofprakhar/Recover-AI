"""Deterministic demo-case reliability tests (post-Phase-12 fix).

Covers the selector, the endpoint, repeatability without a database reset
(the original pain: terminal leftover state made /action/execute NOOP), and
the complete workflow with every persisted artefact asserted.
"""
from decimal import Decimal

from models import (
    AgentAction,
    AuditLog,
    BackgroundJob,
    OrderStatus,
    Payment,
    PaymentEvent,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.demo import demo_eligible_payments


def _payment(db, make_customer, make_order, make_payment, reason="INSUFFICIENT_FUNDS", amount=Decimal("2000.00"), **customer_kwargs):
    customer = make_customer(**customer_kwargs)
    order = make_order(customer=customer, amount=amount)
    payment = make_payment(
        order=order,
        amount=amount,
        status=PaymentStatus.FAILED,
        failure_reason=reason,
    )
    db.commit()
    return payment


def _refresh(db, case):
    db.expire_all()
    return db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()


def test_selector_filters_non_demo_eligible_payments(db, make_customer, make_order, make_payment):
    clean = _payment(db, make_customer, make_order, make_payment)
    no_order = make_payment(order=None, amount=Decimal("100.00"), status=PaymentStatus.FAILED, failure_reason="INSUFFICIENT_FUNDS")
    paid = _payment(db, make_customer, make_order, make_payment)
    paid.order.status = OrderStatus.PAID
    mismatch = _payment(db, make_customer, make_order, make_payment, amount=Decimal("2000.00"))
    mismatch.order.amount = Decimal("9999.00")
    uncertain = _payment(db, make_customer, make_order, make_payment, reason="NETWORK_ERROR")
    db.commit()

    eligible = demo_eligible_payments(db)

    ids = [payment.id for payment in eligible]
    assert ids == [clean.id], "only the clean payment qualifies"
    assert no_order.id not in ids and paid.id not in ids and mismatch.id not in ids and uncertain.id not in ids


def test_selector_excludes_payments_with_any_prior_case(db, make_customer, make_order, make_payment, post_event):
    payment = _payment(db, make_customer, make_order, make_payment)

    post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    assert db.query(RecoveryCase).count() == 1

    assert demo_eligible_payments(db) == []


def test_demo_case_endpoint_returns_executable_case(client, db, make_customer, make_order, make_payment):
    payment = _payment(db, make_customer, make_order, make_payment)

    response = client.post("/api/demo/case")

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["payment_id"] == payment.payment_id
    assert body["case_status"] == "SAFETY_CHECK"
    assert body["selected_action"] == "RETRY_PAYMENT_LINK"
    assert body["score"] >= 80
    case = db.query(RecoveryCase).filter(RecoveryCase.id == body["case_id"]).one()
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["status"] == "SAFETY_CHECK"
    assert detail["payment"]["payment_id"] == payment.payment_id


def test_demo_case_endpoint_skips_agent_escalated_candidates(client, db, make_customer, make_order, make_payment):
    """A RISK_BLOCKED failure creates a clean case the agent escalates - the endpoint must skip it."""
    _payment(db, make_customer, make_order, make_payment, reason="RISK_BLOCKED")
    clean = _payment(db, make_customer, make_order, make_payment)

    response = client.post("/api/demo/case")

    body = response.json()
    assert body["created"] is True
    assert body["payment_id"] == clean.payment_id
    assert body["case_status"] == "SAFETY_CHECK"
    assert len(body["attempted"]) == 1
    assert body["attempted"][0]["case_status"] == "ESCALATED"


def test_demo_case_repeats_without_reset(client, db, make_customer, make_order, make_payment):
    """The original issue: after a completed demo the next case must still be executable - no reset needed."""
    payments = [
        _payment(db, make_customer, make_order, make_payment, amount=Decimal("2000.00"))
        for _ in range(3)
    ]

    first = client.post("/api/demo/case").json()
    assert first["created"] is True
    case_one = db.query(RecoveryCase).filter(RecoveryCase.id == first["case_id"]).one()

    client.post(f"/api/cases/{case_one.id}/action/execute")
    client.post(f"/api/cases/{case_one.id}/outcome", json={"outcome": "SUCCESS"})
    client.post("/api/jobs/run", json={"force": True})
    case_one = _refresh(db, case_one)
    assert case_one.status == RecoveryCaseStatus.RECOVERED

    second = client.post("/api/demo/case").json()
    assert second["created"] is True
    assert second["case_id"] != first["case_id"]
    assert second["payment_id"] != first["payment_id"]
    assert second["case_status"] == "SAFETY_CHECK"
    client.post(f"/api/cases/{second['case_id']}/action/execute")
    case_two = db.query(RecoveryCase).filter(RecoveryCase.id == second["case_id"]).one()
    assert case_two.status == RecoveryCaseStatus.WAITING_FOR_RESULT

    third = client.post("/api/demo/case").json()
    assert third["created"] is True
    assert third["case_id"] not in (first["case_id"], second["case_id"])
    assert third["case_status"] == "SAFETY_CHECK"


def test_demo_case_endpoint_reports_exhausted_pool(client, db, make_customer, make_order, make_payment):
    _payment(db, make_customer, make_order, make_payment)

    first = client.post("/api/demo/case").json()
    assert first["created"] is True

    second = client.post("/api/demo/case").json()
    assert second["created"] is False
    assert second["reason"] == "NO_EXECUTABLE_DEMO_CASE"
    assert "seed --reset" in second["hint"]
    assert db.query(RecoveryCase).count() == 1, "the exhausted result must not create extra cases"


def test_demo_case_full_workflow_persistence(client, db, make_customer, make_order, make_payment):
    """End to end through the APIs: demo case -> execute -> outcome -> verification, then every
    persisted artefact is asserted: case fields, audit logs, agent actions, background jobs, events."""
    payment = _payment(db, make_customer, make_order, make_payment, amount=Decimal("2000.00"))

    demo = client.post("/api/demo/case").json()
    assert demo["case_status"] == "SAFETY_CHECK"
    case_id = demo["case_id"]

    executed = client.post(f"/api/cases/{case_id}/action/execute").json()
    assert executed["decision"] == "WAITING_FOR_RESULT"
    assert executed["execution"]["recovery_payment_id"] == f"pay_rec_{case_id}_1"
    assert executed["execution"]["simulated"] is True

    outcome = client.post(f"/api/cases/{case_id}/outcome", json={"outcome": "SUCCESS"}).json()
    assert outcome["decision"] == "WAITING_FOR_RESULT"
    assert outcome["outcome"]["simulated"] is True

    jobs_run = client.post("/api/jobs/run", json={"force": True}).json()
    executed_jobs = {item["job_key"]: item for item in jobs_run["executed"]}
    assert executed_jobs[f"verify:{case_id}:1:outcome"]["result"]["decision"] == "RECOVERED"

    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).one()
    assert case.status == RecoveryCaseStatus.RECOVERED
    assert case.recovered_payment_id == f"pay_rec_{case_id}_1"
    assert case.recovered_amount == Decimal("2000.00")
    assert case.recovered_at is not None
    assert case.attempt_count == 1

    detail = client.get(f"/api/cases/{case_id}").json()
    assert detail["status"] == "RECOVERED"
    assert detail["recovered_payment_id"] == f"pay_rec_{case_id}_1"
    assert detail["recovered_amount"] == "2000.00"
    assert detail["recovered_at"] is not None

    events = [log.event_type for log in db.query(AuditLog).filter(AuditLog.case_id == case_id).order_by(AuditLog.id).all()]
    for expected in (
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
    ):
        assert expected in events, f"missing audit event {expected}"
    assert events.index("gate.allowed") < events.index("action.executed") < events.index("verification.recovered")
    recovered_log = (
        db.query(AuditLog)
        .filter(AuditLog.case_id == case_id, AuditLog.event_type == "verification.recovered")
        .one()
    )
    assert recovered_log.from_status == "WAITING_FOR_RESULT"
    assert recovered_log.to_status == "RECOVERED"
    assert recovered_log.payload["rule"] == 12
    assert recovered_log.payload["payment_id"] == f"pay_rec_{case_id}_1"
    assert recovered_log.payload["amount"] == "2000.00"
    assert recovered_log.payload["simulated"] is True
    assert recovered_log.payload["attribution_checks"]["after_approved_action"] is True

    actions = {
        row.tool_name: row
        for row in db.query(AgentAction).filter(AgentAction.case_id == case_id).all()
    }
    assert actions["get_payment_status"].allowed is None
    assert actions["safety_gate"].allowed is True
    assert actions["execute_recovery_action"].allowed is True
    assert actions["execute_recovery_action"].output["recovery_payment_id"] == f"pay_rec_{case_id}_1"
    assert actions["simulate_outcome"].output["simulated"] is True
    assert actions["verify_outcome"].output["decision"] == "RECOVERED"

    jobs = {
        row.job_key: row
        for row in db.query(BackgroundJob).filter(BackgroundJob.params["case_id"].as_integer() == case_id).all()
    }
    assert jobs[f"agent:{case_id}:0"].status == "DONE"
    assert jobs[f"verify:{case_id}:1:outcome"].status == "DONE"
    assert jobs[f"verify:{case_id}:1:executed"].status == "DONE"
    assert jobs[f"verify:{case_id}:1:executed"].result["decision"] == "NOOP"

    recovery_payment = db.query(Payment).filter(Payment.payment_id == f"pay_rec_{case_id}_1").one()
    assert recovery_payment.status == PaymentStatus.CAPTURED
    assert recovery_payment.order.status == OrderStatus.PAID
    assert payment.status == PaymentStatus.FAILED, "the source payment keeps its failed state"

    event_rows = db.query(PaymentEvent).order_by(PaymentEvent.id).all()
    event_types = [row.event_type for row in event_rows]
    assert "payment.failed" in event_types
    assert "payment.captured" in event_types
    failed_event = next(row for row in event_rows if row.event_type == "payment.failed")
    captured_event = next(row for row in event_rows if row.event_type == "payment.captured")
    assert failed_event.payment_ref == payment.payment_id
    assert failed_event.processing_status == "PROCESSED"
    assert captured_event.payment_ref == f"pay_rec_{case_id}_1"
    assert captured_event.source == "REPLAY"
    assert captured_event.processing_status == "PROCESSED"

    metrics = client.get("/api/metrics").json()
    assert metrics["cases"]["by_status"] == {"RECOVERED": 1}
    assert metrics["recovered"]["revenue"] == "2000.00"
    assert metrics["recovered"]["cases"] == 1
    assert metrics["recovery_rate"] == 1.0
    assert metrics["attempts"] == {"total": 1, "successful_recoveries": 1}
