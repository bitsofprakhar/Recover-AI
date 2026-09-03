"""Phase 9 tests: outcome verification, attribution and recovery metrics."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from models import (
    AgentAction,
    AuditLog,
    OrderStatus,
    Payment,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.action_executor import execute_action
from services.agent import run_agent
from services.agent.tools import execute_tool
from services.metrics import compute_metrics
from services.outcome_simulator import simulate_outcome
from services.verification import verify_outcome


def _make_case(db, make_customer, make_order, make_payment, post_event, amount=Decimal("2000.00"), **customer_kwargs):
    customer = make_customer(**customer_kwargs)
    order = make_order(customer=customer, amount=amount)
    payment = make_payment(order=order, amount=amount, status=PaymentStatus.PENDING)
    result = post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    assert result["risk_evaluation"]["decision"] == "CASE_CREATED"
    case = db.query(RecoveryCase).filter(RecoveryCase.payment_id == payment.id).one()
    db.expire_all()
    return case


def _to_waiting(db, case):
    run_agent(db, case.id)
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    return case


def _refresh(db, case):
    db.expire_all()
    return db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()


def _actions(db, case):
    return db.query(AgentAction).filter(AgentAction.case_id == case.id).order_by(AgentAction.id).all()


def _audits(db, case):
    return db.query(AuditLog).filter(AuditLog.case_id == case.id).order_by(AuditLog.id).all()


def _source_payment(db, case):
    return db.query(Payment).filter(Payment.id == case.payment_id).one()


def _recovered_loop(db, case):
    """Execute, script a successful outcome and verify - the full Phase 9 close."""
    execute_action(db, case)
    simulate_outcome(db, _refresh(db, case), "SUCCESS")
    return verify_outcome(db, _refresh(db, case))


def test_verify_success_attributes_recovery_payment_and_recovers(
    db, make_customer, make_order, make_payment, post_event
):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)
    simulate_outcome(db, _refresh(db, case), "SUCCESS")

    result = verify_outcome(db, _refresh(db, case))

    assert result["decision"] == "RECOVERED"
    assert result["verification"]["result"] == "RECOVERED"
    attribution = result["verification"]["attribution"]
    assert attribution["payment_id"] == f"pay_rec_{case.id}_1"
    assert attribution["amount"] == "2000.00"
    assert attribution["event_source"] == "REPLAY"
    assert attribution["simulated"] is True
    assert attribution["recovery_case_payment"] is True
    assert attribution["checks"] == {
        "associated": True,
        "after_approved_action": True,
        "within_case_window": True,
        "not_already_credited": True,
    }
    assert result["verification"]["recovery"]["recovered"] is True
    assert result["verification"]["recovery"]["amount"] == "2000.00"

    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.RECOVERED
    assert case.recovered_payment_id == f"pay_rec_{case.id}_1"
    assert case.recovered_amount == Decimal("2000.00")
    assert case.recovered_at is not None

    events = [(log.event_type, log.from_status, log.to_status) for log in _audits(db, case)]
    assert events[-1] == ("verification.recovered", "WAITING_FOR_RESULT", "RECOVERED")
    payload = _audits(db, case)[-1].payload
    assert payload["rule"] == 12
    assert payload["payment_id"] == f"pay_rec_{case.id}_1"
    assert payload["attribution_checks"]["after_approved_action"] is True
    assert payload["simulated"] is True

    verify_rows = [a for a in _actions(db, case) if a.tool_name == "verify_outcome"]
    assert len(verify_rows) == 1
    assert verify_rows[0].allowed is None
    assert verify_rows[0].input["case_status"] == "WAITING_FOR_RESULT"


def test_verify_is_final_and_never_transitions_a_recovered_case(
    db, make_customer, make_order, make_payment, post_event
):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    first = _recovered_loop(db, case)

    second = verify_outcome(db, _refresh(db, case))

    assert second["decision"] == "NOOP"
    assert "terminal" in second["reason"]
    assert second["verification"]["recovered"] is True
    assert second["verification"]["payment_id"] == first["verification"]["attribution"]["payment_id"]
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.RECOVERED
    assert case.recovered_amount == Decimal("2000.00")
    assert len([a for a in _actions(db, case) if a.tool_name == "verify_outcome"]) == 1
    assert [log.event_type for log in _audits(db, case)].count("verification.recovered") == 1


def test_verify_notification_only_success_attributes_customer_retry(
    db, make_customer, make_order, make_payment, post_event
):
    customer = make_customer(lifetime_payments=10, lifetime_successes=4)
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    case = db.query(RecoveryCase).one()
    run_agent(db, case.id)
    case = _refresh(db, case)
    assert case.selected_action == "SEND_NOTIFICATION_ONLY"
    execute_action(db, case)
    simulate_outcome(db, _refresh(db, case), "SUCCESS")

    result = verify_outcome(db, _refresh(db, case))

    assert result["decision"] == "RECOVERED"
    case = _refresh(db, case)
    assert case.recovered_payment_id == f"pay_retry_{case.id}_1"
    assert case.recovered_amount == Decimal("2000.00")
    attribution = result["verification"]["attribution"]
    assert attribution["recovery_case_payment"] is False
    assert attribution["checks"]["associated"] is True
    events = [log.event_type for log in _audits(db, case)]
    assert events[-1] == "verification.recovered"


def test_verify_monitor_action_success_within_window_recovers(
    db, make_customer, make_order, make_payment, post_event
):
    customer = make_customer(lifetime_payments=10, lifetime_successes=2)
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    case = db.query(RecoveryCase).one()
    run_agent(db, case.id)
    case = _refresh(db, case)
    assert case.selected_action == "WAIT_AND_MONITOR"
    execute_action(db, case)
    simulate_outcome(db, _refresh(db, case), "SUCCESS")

    result = verify_outcome(db, _refresh(db, case))

    assert result["decision"] == "RECOVERED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.RECOVERED
    assert case.recovered_payment_id == f"pay_retry_{case.id}_0"
    assert case.attempt_count == 0


def test_verify_without_success_keeps_monitoring(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)

    result = verify_outcome(db, _refresh(db, case))

    assert result["decision"] == "WAITING_FOR_RESULT"
    assert result["verification"]["result"] == "NO_SUCCESS_YET"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.WAITING_FOR_RESULT
    assert case.recovered_payment_id is None
    assert [a.tool_name for a in _actions(db, case)][-1] == "verify_outcome"
    assert not [log for log in _audits(db, case) if log.event_type.startswith("verification.")]

    again = verify_outcome(db, _refresh(db, case))
    assert again["decision"] == "WAITING_FOR_RESULT"
    assert len([a for a in _actions(db, case) if a.tool_name == "verify_outcome"]) == 2


def test_verify_on_expired_case_stops(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)
    case = _refresh(db, case)
    case.expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    result = verify_outcome(db, _refresh(db, case))

    assert result["decision"] == "STOPPED"
    assert _refresh(db, case).status == RecoveryCaseStatus.STOPPED
    assert _refresh(db, case).recovered_payment_id is None
    log = _audits(db, case)[-1]
    assert log.event_type == "case.window_expired"
    assert log.to_status == "STOPPED"
    assert log.payload["detected_by"] == "verification"


def test_verify_success_not_attributable_is_not_recovered(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)
    source = _source_payment(db, case)

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    stale = Payment(
        payment_id="pay_stale_1",
        order_id=source.order_id,
        amount=Decimal("2000.00"),
        method="UPI",
        status=PaymentStatus.CAPTURED,
        gateway_metadata={"gateway": "razorpay", "mode": "test"},
        created_at=past,
        updated_at=past,
    )
    db.add(stale)
    db.commit()

    result = verify_outcome(db, _refresh(db, case))

    assert result["decision"] == "NOT_RECOVERED"
    assert result["verification"]["result"] == "SUCCESS_NOT_ATTRIBUTABLE"
    candidate = result["verification"]["candidates"][0]
    assert candidate["payment_id"] == "pay_stale_1"
    assert candidate["checks"]["after_approved_action"] is False
    assert candidate["rejection_reason"] == "CAPTURED_BEFORE_APPROVED_ACTION"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.NOT_RECOVERED
    assert case.recovered_payment_id is None
    assert case.recovered_amount is None
    log = _audits(db, case)[-1]
    assert log.event_type == "verification.not_recovered"
    assert log.to_status == "NOT_RECOVERED"
    assert log.payload["reason"] == "SUCCESS_NOT_ATTRIBUTABLE"
    assert log.payload["rule"] == 18


def test_verify_rejects_non_waiting_and_terminal_cases(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    result = verify_outcome(db, case)
    assert result["decision"] == "NOOP"
    assert "waiting" in result["reason"]
    assert result["verification"] is None

    case = _to_waiting(db, case)
    result = verify_outcome(db, case)
    assert result["decision"] == "NOOP"
    assert "waiting" in result["reason"]

    recovered = _recovered_loop(db, case)
    assert recovered["decision"] == "RECOVERED"
    result = verify_outcome(db, _refresh(db, case))
    assert result["decision"] == "NOOP"
    assert result["verification"]["recovered"] is True


def test_verified_payment_is_never_credited_twice(db, make_customer, make_order, make_payment, post_event):
    case_one = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    _recovered_loop(db, case_one)
    case_one = _refresh(db, case_one)
    source = _source_payment(db, case_one)
    assert case_one.recovered_payment_id == f"pay_rec_{case_one.id}_1"

    extra = Payment(
        payment_id=f"pay_extra_{case_one.id}",
        order_id=source.order_id,
        amount=Decimal("2000.00"),
        method="UPI",
        status=PaymentStatus.FAILED,
        failure_reason="INSUFFICIENT_FUNDS",
        gateway_metadata={"gateway": "razorpay", "mode": "test"},
    )
    db.add(extra)
    db.flush()
    case_two = RecoveryCase(
        payment_id=extra.id,
        revenue_at_risk=Decimal("2000.00"),
        status=RecoveryCaseStatus.WAITING_FOR_RESULT,
        attempt_count=1,
        selected_action="RETRY_PAYMENT_LINK",
        expiry=datetime.now(timezone.utc) + timedelta(hours=12),
    )
    db.add(case_two)
    db.flush()
    db.add(
        AuditLog(
            event_type="action.executed",
            case_id=case_two.id,
            from_status="SAFETY_CHECK",
            to_status="ACTION_EXECUTED",
            payload={"action": "RETRY_PAYMENT_LINK", "attempt_count": 1, "simulated": True},
            created_at=case_one.recovered_at - timedelta(seconds=1),
        )
    )
    db.commit()

    result = verify_outcome(db, _refresh(db, case_two))

    assert result["decision"] == "NOT_RECOVERED"
    candidate = result["verification"]["candidates"][0]
    assert candidate["payment_id"] == case_one.recovered_payment_id
    assert candidate["checks"]["not_already_credited"] is False
    assert candidate["rejection_reason"] == "ALREADY_CREDITED_TO_ANOTHER_CASE"
    case_two = _refresh(db, case_two)
    assert case_two.recovered_payment_id is None

    case_two.recovered_payment_id = case_one.recovered_payment_id
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    metrics = compute_metrics(db)
    assert metrics["recovered"]["revenue"] == "2000.00"
    assert metrics["recovered"]["cases"] == 1
    assert metrics["attempts"]["successful_recoveries"] == 1


def test_verify_escalates_conflicting_order_state(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)
    source = _source_payment(db, case)
    source.order.status = OrderStatus.PAID
    db.commit()

    result = verify_outcome(db, _refresh(db, case))

    assert result["decision"] == "ESCALATED"
    assert result["verification"]["result"] == "CONFLICTING_ORDER_STATE"
    assert _refresh(db, case).status == RecoveryCaseStatus.ESCALATED
    log = _audits(db, case)[-1]
    assert log.event_type == "verification.escalated"
    assert log.to_status == "ESCALATED"


def test_verify_escalates_missing_order_identity(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)
    source = _source_payment(db, case)
    source.order_id = None
    db.commit()

    result = verify_outcome(db, _refresh(db, case))

    assert result["decision"] == "ESCALATED"
    assert result["verification"]["result"] == "MISSING_ORDER_IDENTITY"
    assert _refresh(db, case).status == RecoveryCaseStatus.ESCALATED


def test_check_recovery_result_reports_attribution(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    before = execute_tool(db, case, "check_recovery_result", {})
    assert before["case_recovered"] is False
    assert before["recovered_payment_id"] is None
    assert before["recovered_amount"] is None

    case = _to_waiting(db, case)
    _recovered_loop(db, case)

    after = execute_tool(db, _refresh(db, case), "check_recovery_result", {})
    assert after["case_recovered"] is True
    assert after["recovered_payment_id"] == f"pay_rec_{case.id}_1"
    assert after["recovered_amount"] == "2000.00"
    assert after["recovered_at"] is not None
    assert after["recovery_payment"]["status"] == "CAPTURED"


def test_metrics_computed_from_stored_data(db, make_customer, make_order, make_payment, post_event):
    case_a = _make_case(db, make_customer, make_order, make_payment, post_event, amount=Decimal("2000.00"))
    _recovered_loop(db, _to_waiting(db, case_a))

    case_b = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event, amount=Decimal("3000.00")))
    execute_action(db, case_b)
    simulate_outcome(db, _refresh(db, case_b), "FAILED")
    run_agent(db, case_b.id)
    execute_action(db, _refresh(db, case_b))
    simulate_outcome(db, _refresh(db, case_b), "FAILED")
    assert _refresh(db, case_b).status == RecoveryCaseStatus.NOT_RECOVERED

    customer_c = make_customer()
    order_c = make_order(customer=customer_c, amount=Decimal("1500.00"))
    payment_c = make_payment(order=order_c, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    result_c = post_event(
        {"payment_id": payment_c.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    assert result_c["risk_evaluation"]["decision"] == "CASE_ESCALATED"

    case_d = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event, amount=Decimal("1000.00")))
    execute_action(db, case_d)
    assert _refresh(db, case_d).status == RecoveryCaseStatus.WAITING_FOR_RESULT

    db.add(
        AgentAction(
            case_id=case_a.id,
            tool_name="safety_gate",
            input={"idempotency_key": "gate:blocked:example"},
            output={"decision": "BLOCK", "reason": "ATTEMPT_LIMIT_REACHED"},
            allowed=False,
        )
    )
    execute_tool(db, _refresh(db, case_d), "create_recovery_payment", {}, authorized=False)
    db.commit()

    metrics = compute_metrics(db)

    assert metrics["cases"]["total"] == 4
    assert metrics["cases"]["active"] == 1
    assert metrics["cases"]["by_status"] == {
        "ESCALATED": 1,
        "NOT_RECOVERED": 1,
        "RECOVERED": 1,
        "WAITING_FOR_RESULT": 1,
    }
    assert metrics["revenue_at_risk"]["total"] == "7500.00"
    assert metrics["revenue_at_risk"]["eligible"] == "6000.00"
    assert metrics["revenue_at_risk"]["escalated_excluded"] == "1500.00"
    assert metrics["recovered"]["revenue"] == "2000.00"
    assert metrics["recovered"]["cases"] == 1
    assert metrics["recovery_rate"] == pytest.approx(2000 / 6000)
    assert metrics["attempts"] == {"total": 4, "successful_recoveries": 1}
    assert metrics["average_recovery_time"] is not None
    assert metrics["average_recovery_time"]["cases_counted"] == 1
    assert metrics["average_recovery_time"]["seconds"] > 0
    assert metrics["escalation_rate"] == pytest.approx(0.25)
    assert metrics["invalid_or_blocked_actions"]["total"] == 2
    assert metrics["invalid_or_blocked_actions"]["by_tool"] == {"create_recovery_payment": 1, "safety_gate": 1}


def test_metrics_on_empty_database(db):
    metrics = compute_metrics(db)

    assert metrics["cases"] == {"total": 0, "active": 0, "by_status": {}}
    assert metrics["revenue_at_risk"]["total"] == "0"
    assert metrics["revenue_at_risk"]["eligible"] == "0"
    assert metrics["recovered"] == {"revenue": "0", "cases": 0}
    assert metrics["recovery_rate"] is None
    assert metrics["attempts"] == {"total": 0, "successful_recoveries": 0}
    assert metrics["average_recovery_time"] is None
    assert metrics["escalation_rate"] is None
    assert metrics["invalid_or_blocked_actions"] == {"total": 0, "by_tool": {}}


def test_verify_and_metrics_api_endpoints(client, db, make_customer, make_order, make_payment, post_event):
    response = client.post("/api/cases/999/verify")
    assert response.status_code == 404

    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)
    simulate_outcome(db, _refresh(db, case), "SUCCESS")

    response = client.post(f"/api/cases/{case.id}/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "RECOVERED"
    assert body["verification"]["attribution"]["payment_id"] == f"pay_rec_{case.id}_1"
    assert body["verification"]["recovery"]["amount"] == "2000.00"

    response = client.post(f"/api/cases/{case.id}/verify")
    assert response.status_code == 200
    assert response.json()["decision"] == "NOOP"

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["status"] == "RECOVERED"
    assert detail["recovered_payment_id"] == f"pay_rec_{case.id}_1"
    assert detail["recovered_amount"] == "2000.00"
    assert detail["recovered_at"] is not None
    names = [action["tool_name"] for action in detail["agent_actions"]]
    assert names[-2:] == ["simulate_outcome", "verify_outcome"]
    events = [log["event_type"] for log in detail["audit_logs"]]
    assert events[-2:] == ["outcome.success", "verification.recovered"]

    response = client.get("/api/metrics")
    assert response.status_code == 200
    metrics = response.json()
    assert metrics["cases"]["total"] == 1
    assert metrics["recovered"]["revenue"] == "2000.00"
    assert metrics["recovery_rate"] == pytest.approx(1.0)
    assert metrics["invalid_or_blocked_actions"]["total"] == 0
