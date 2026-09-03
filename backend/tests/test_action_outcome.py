"""Phase 8 tests: recovery action execution (simulated) and the outcome simulator."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
from services.outcome_simulator import simulate_outcome


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


def _to_waiting(db, case, **customer_kwargs):
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


def _recovery_payments(db, case):
    case = _refresh(db, case)
    source = db.query(Payment).filter(Payment.id == case.payment_id).one()
    rows = db.query(Payment).filter(Payment.order_id == source.order_id).order_by(Payment.id).all()
    return [row for row in rows if (row.gateway_metadata or {}).get("recovery_case_id") == case.id]


def test_execute_retry_action_creates_simulated_payment_and_notification(
    db, make_customer, make_order, make_payment, post_event
):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))

    result = execute_action(db, case)

    assert result["decision"] == "WAITING_FOR_RESULT"
    execution = result["execution"]
    assert execution["executed"] is True
    assert execution["simulated"] is True
    assert execution["action"] == "RETRY_PAYMENT_LINK"
    assert execution["attempt_count"] == 1
    assert execution["recovery_payment_id"] == f"pay_rec_{case.id}_1"
    assert execution["recovery_link_id"] == f"rlink_{case.id}_1"
    assert execution["notification_channel"] == "EMAIL"
    assert [call["tool_name"] for call in execution["tool_calls"]] == [
        "create_recovery_payment",
        "send_recovery_notification",
    ]

    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.WAITING_FOR_RESULT
    assert case.attempt_count == 1

    recovery = _recovery_payments(db, case)
    assert len(recovery) == 1
    row = recovery[0]
    assert row.status == PaymentStatus.PENDING
    assert row.amount == Decimal("2000.00")
    assert row.method == "UPI"
    assert row.gateway_metadata["simulated"] is True
    assert row.gateway_metadata["recovery_case_id"] == case.id
    assert row.gateway_metadata["recovery_link_id"] == f"rlink_{case.id}_1"

    names = [action.tool_name for action in _actions(db, case)]
    assert names[-4:] == [
        "safety_gate",
        "create_recovery_payment",
        "send_recovery_notification",
        "execute_recovery_action",
    ]
    create_row = [a for a in _actions(db, case) if a.tool_name == "create_recovery_payment"][-1]
    assert create_row.allowed is True
    assert create_row.output["executed"] is True
    assert create_row.output["simulated"] is True
    notify_row = [a for a in _actions(db, case) if a.tool_name == "send_recovery_notification"][-1]
    assert notify_row.allowed is True
    assert notify_row.output["channel"] == "EMAIL"
    assert notify_row.output["recipient_masked"].startswith("cu")
    assert "@" in notify_row.output["recipient_masked"]

    events = [(log.event_type, log.from_status, log.to_status) for log in _audits(db, case)]
    assert events[-3:] == [
        ("gate.allowed", "SAFETY_CHECK", None),
        ("action.executed", "SAFETY_CHECK", "ACTION_EXECUTED"),
        ("action.monitoring_started", "ACTION_EXECUTED", "WAITING_FOR_RESULT"),
    ]


def test_execution_is_idempotent(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    first = execute_action(db, case)
    db.expire_all()

    second = execute_action(db, _refresh(db, case))

    assert second["decision"] == "WAITING_FOR_RESULT"
    assert second["execution"]["replay"] is True
    assert second["execution"]["idempotency_key"] == first["execution"]["idempotency_key"]
    assert len(_recovery_payments(db, case)) == 1
    exec_rows = [a for a in _actions(db, case) if a.tool_name == "execute_recovery_action"]
    assert len(exec_rows) == 1
    events = [log.event_type for log in _audits(db, case)]
    assert events.count("action.executed") == 1


def test_execute_notification_only_action(db, make_customer, make_order, make_payment, post_event):
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

    result = execute_action(db, case)

    assert result["execution"]["action"] == "SEND_NOTIFICATION_ONLY"
    assert result["execution"]["recovery_payment_id"] is None
    assert [call["tool_name"] for call in result["execution"]["tool_calls"]] == [
        "send_recovery_notification"
    ]
    assert _refresh(db, case).attempt_count == 1
    assert _recovery_payments(db, case) == []


def test_execute_monitor_action_records_no_attempt(db, make_customer, make_order, make_payment, post_event):
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

    result = execute_action(db, case)

    assert result["execution"]["action"] == "WAIT_AND_MONITOR"
    assert result["execution"]["recovery_payment_id"] is None
    assert result["execution"]["notification_channel"] is None
    assert [call["tool_name"] for call in result["execution"]["tool_calls"]] == ["monitor"]
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.WAITING_FOR_RESULT
    assert case.attempt_count == 0
    assert _recovery_payments(db, case) == []
    events = [(log.event_type, log.to_status) for log in _audits(db, case)]
    assert events[-2:] == [("action.executed", "ACTION_EXECUTED"), ("action.monitoring_started", "WAITING_FOR_RESULT")]


def test_execute_requires_safety_check_status(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    result = execute_action(db, case)

    assert result["decision"] == "NOOP"
    assert "SAFETY_CHECK" in result["reason"]
    assert result["execution"] is None
    assert _actions(db, case) == []

    case.status = RecoveryCaseStatus.RECOVERED
    db.commit()
    result = execute_action(db, case)
    assert result["decision"] == "NOOP"
    assert "terminal" in result["reason"]


def test_execution_reverifies_gate_and_stops_expired_case(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    case.expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    result = execute_action(db, _refresh(db, case))

    assert result["decision"] == "STOPPED"
    assert "CASE_WINDOW_EXPIRED" in result["reason"]
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.STOPPED
    assert case.attempt_count == 0
    assert _recovery_payments(db, case) == []
    recheck = [a for a in _actions(db, case) if a.tool_name == "safety_gate"][-1]
    assert recheck.output["decision"] == "BLOCK"
    assert recheck.output["recheck"] is True
    assert recheck.allowed is False
    events = [log.event_type for log in _audits(db, case)]
    assert events[-1] == "gate.case_stopped"


def test_execution_reverifies_gate_and_escalates_captured_payment(
    db, make_customer, make_order, make_payment, post_event
):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.status = PaymentStatus.CAPTURED
    db.commit()

    result = execute_action(db, _refresh(db, case))

    assert result["decision"] == "ESCALATED"
    assert "PAYMENT_ALREADY_SUCCESSFUL" in result["reason"]
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.ESCALATED
    assert case.attempt_count == 0
    assert _recovery_payments(db, case) == []


def test_unauthorized_act_tool_still_blocked_after_executor_exists(
    db, make_customer, make_order, make_payment, post_event
):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))

    out = execute_tool(db, case, "create_recovery_payment", {}, authorized=False)

    assert out["status"] == "BLOCKED"
    assert out["reason"] == "GATE_AUTHORIZATION_REQUIRED"
    assert out["executed"] is False
    assert _recovery_payments(db, case) == []


def test_check_recovery_result_reports_recovery_payment(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))

    before = execute_tool(db, case, "check_recovery_result", {})
    assert before["recovery_action_executed"] is False
    assert before["recovery_payment"] is None

    execute_action(db, case)

    after = execute_tool(db, _refresh(db, case), "check_recovery_result", {})
    assert after["recovery_action_executed"] is True
    assert after["recovery_payment"]["payment_id"] == f"pay_rec_{case.id}_1"
    assert after["recovery_payment"]["status"] == "PENDING"
    assert after["recovery_payment"]["simulated"] is True


def test_outcome_success_captures_recovery_payment(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)

    result = simulate_outcome(db, _refresh(db, case), "SUCCESS")

    assert result["decision"] == "WAITING_FOR_RESULT"
    assert result["outcome"]["simulated"] is True
    assert result["outcome"]["event_processing"]["processing_status"] == "PROCESSED"
    assert result["outcome"]["event_processing"]["payment_status"] == "CAPTURED"

    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.WAITING_FOR_RESULT
    recovery = _recovery_payments(db, case)
    assert recovery[0].status == PaymentStatus.CAPTURED
    source = db.query(Payment).filter(Payment.id == case.payment_id).one()
    assert source.order.status == OrderStatus.PAID

    events = [log.event_type for log in _audits(db, case)]
    assert "outcome.success" in events
    assert events[-1] == "outcome.success"

    again = simulate_outcome(db, case, "SUCCESS")
    assert again["decision"] == "NOOP"
    assert "terminal state" in again["reason"]


def test_outcome_success_on_notification_only_simulates_customer_retry(
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
    execute_action(db, _refresh(db, case))
    assert _recovery_payments(db, case) == []

    result = simulate_outcome(db, _refresh(db, case), "SUCCESS")

    assert result["decision"] == "WAITING_FOR_RESULT"
    retry_id = result["outcome"]["event_processing"]["payment_id"]
    assert retry_id == f"pay_retry_{case.id}_1"
    assert result["outcome"]["event_processing"]["payment_status"] == "CAPTURED"
    retry = db.query(Payment).filter(Payment.payment_id == retry_id).one()
    assert retry.status == PaymentStatus.CAPTURED
    assert retry.order_id == order.id
    assert _refresh(db, case).status == RecoveryCaseStatus.WAITING_FOR_RESULT


def test_outcome_failed_retries_then_not_recovered(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)

    first = simulate_outcome(db, _refresh(db, case), "FAILED")

    assert first["decision"] == "DIAGNOSING"
    assert first["outcome"]["event_processing"]["payment_status"] == "FAILED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.DIAGNOSING
    assert case.attempt_count == 1
    recovery = _recovery_payments(db, case)
    assert recovery[0].status == PaymentStatus.FAILED

    run_agent(db, case.id)
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    assert case.attempt_count == 1
    execute_action(db, case)
    case = _refresh(db, case)
    assert case.attempt_count == 2
    assert len(_recovery_payments(db, case)) == 2

    second = simulate_outcome(db, case, "FAILED")

    assert second["decision"] == "NOT_RECOVERED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.NOT_RECOVERED
    events = [log.event_type for log in _audits(db, case)]
    assert events.count("outcome.retry") == 1
    assert events[-1] == "outcome.not_recovered"


def test_outcome_pending_and_no_response_keep_monitoring(
    db, make_customer, make_order, make_payment, post_event
):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)

    pending = simulate_outcome(db, _refresh(db, case), "STILL_PENDING")
    assert pending["decision"] == "WAITING_FOR_RESULT"
    assert _refresh(db, case).status == RecoveryCaseStatus.WAITING_FOR_RESULT
    assert _recovery_payments(db, case)[0].status == PaymentStatus.PENDING

    no_response = simulate_outcome(db, _refresh(db, case), "NO_RESPONSE")
    assert no_response["decision"] == "WAITING_FOR_RESULT"
    assert _refresh(db, case).status == RecoveryCaseStatus.WAITING_FOR_RESULT

    events = [log.event_type for log in _audits(db, case)]
    assert "outcome.still_pending" in events
    assert "outcome.no_response" in events


def test_outcome_on_expired_case_stops(db, make_customer, make_order, make_payment, post_event):
    case = _to_waiting(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    execute_action(db, case)
    case = _refresh(db, case)
    case.expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    result = simulate_outcome(db, _refresh(db, case), "SUCCESS")

    assert result["decision"] == "STOPPED"
    assert _refresh(db, case).status == RecoveryCaseStatus.STOPPED
    events = [log.event_type for log in _audits(db, case)]
    assert events[-1] == "case.window_expired"
    assert _recovery_payments(db, case)[0].status == PaymentStatus.PENDING


def test_outcome_requires_waiting_status(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    result = simulate_outcome(db, case, "SUCCESS")

    assert result["decision"] == "NOOP"
    assert "WAITING_FOR_RESULT" in result["reason"] or "waiting" in result["reason"]
    assert result["outcome"] is None


def test_execute_and_outcome_api_endpoints(client, db, make_customer, make_order, make_payment, post_event):
    response = client.post("/api/cases/999/action/execute")
    assert response.status_code == 404
    response = client.post("/api/cases/999/outcome", json={"outcome": "SUCCESS"})
    assert response.status_code == 404
    response = client.post("/api/cases/1/outcome", json={"outcome": "BOGUS"})
    assert response.status_code in (404, 422)

    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    run_agent(db, case.id)

    response = client.post(f"/api/cases/{case.id}/action/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "WAITING_FOR_RESULT"
    assert body["execution"]["simulated"] is True
    assert body["execution"]["recovery_payment_id"] == f"pay_rec_{case.id}_1"

    response = client.post(f"/api/cases/{case.id}/action/execute")
    assert response.status_code == 200
    assert response.json()["execution"]["replay"] is True

    response = client.post(f"/api/cases/{case.id}/outcome", json={"outcome": "SUCCESS"})
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "WAITING_FOR_RESULT"
    assert body["outcome"]["simulated"] is True
    assert body["outcome"]["event_processing"]["payment_status"] == "CAPTURED"

    response = client.post(f"/api/cases/{case.id}/outcome", json={"outcome": "BOGUS"})
    assert response.status_code == 422

    detail = client.get(f"/api/cases/{case.id}").json()
    names = [action["tool_name"] for action in detail["agent_actions"]]
    assert names[-5:] == [
        "safety_gate",
        "create_recovery_payment",
        "send_recovery_notification",
        "execute_recovery_action",
        "simulate_outcome",
    ]
    events = [log["event_type"] for log in detail["audit_logs"]]
    assert events[-3:] == ["action.executed", "action.monitoring_started", "outcome.success"]
    assert detail["status"] == "WAITING_FOR_RESULT"
