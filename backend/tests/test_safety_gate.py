"""Phase 7 tests: safety/policy gate decisions, limits and idempotency."""
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
from services.agent import run_agent
from services.safety_gate import evaluate, submit_to_gate


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


def _selected(db, case, action="RETRY_PAYMENT_LINK", attempt_count=0):
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()
    case.status = RecoveryCaseStatus.ACTION_SELECTED
    case.selected_action = action
    case.attempt_count = attempt_count
    db.commit()
    return case


def _gate_actions(db, case):
    return (
        db.query(AgentAction)
        .filter(AgentAction.case_id == case.id, AgentAction.tool_name == "safety_gate")
        .order_by(AgentAction.id)
        .all()
    )


def _audit(db, case):
    return db.query(AuditLog).filter(AuditLog.case_id == case.id).order_by(AuditLog.id).all()


def _refresh(db, case):
    db.expire_all()
    return db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()


def test_gate_allows_clean_selected_action(db, make_customer, make_order, make_payment, post_event):
    case = _selected(db, _make_case(db, make_customer, make_order, make_payment, post_event))

    result = submit_to_gate(db, case)

    assert result["decision"] == "ALLOW"
    gate = result["gate"]
    assert gate["reason"] == "ALL_CHECKS_PASSED"
    assert [(c["name"], c["result"]) for c in gate["checks"]] == [
        ("case_window", "PASS"),
        ("action_catalog", "PASS"),
        ("payment_state", "PASS"),
        ("order_identity", "PASS"),
        ("amount_match", "PASS"),
        ("order_state", "PASS"),
        ("duplicate_active_case", "PASS"),
        ("attempt_limit", "PASS"),
    ]
    assert gate["idempotency_key"] == f"gate:{case.id}:RETRY_PAYMENT_LINK:0"

    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    assert case.selected_action == "RETRY_PAYMENT_LINK"

    rows = _gate_actions(db, case)
    assert len(rows) == 1
    assert rows[0].allowed is True
    assert rows[0].input == {"idempotency_key": gate["idempotency_key"], "action": "RETRY_PAYMENT_LINK"}
    assert rows[0].output["decision"] == "ALLOW"

    events = [(log.event_type, log.from_status, log.to_status) for log in _audit(db, case)]
    assert events[-2:] == [
        ("gate.submitted", "ACTION_SELECTED", "SAFETY_CHECK"),
        ("gate.allowed", "SAFETY_CHECK", None),
    ]


def test_gate_submission_is_idempotent(db, make_customer, make_order, make_payment, post_event):
    case = _selected(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    first = submit_to_gate(db, case)
    db.commit()

    second = submit_to_gate(db, case)

    assert second["decision"] == "ALLOW"
    assert second["gate"]["replay"] is True
    assert second["gate"]["idempotency_key"] == first["gate"]["idempotency_key"]
    assert len(_gate_actions(db, case)) == 1
    events = [log.event_type for log in _audit(db, case)]
    assert events.count("gate.allowed") == 1
    assert events.count("gate.submitted") == 1
    assert _refresh(db, case).status == RecoveryCaseStatus.SAFETY_CHECK


def test_attempt_limit_blocks_with_monitor_alternative(db, make_customer, make_order, make_payment, post_event):
    case = _selected(db, _make_case(db, make_customer, make_order, make_payment, post_event), attempt_count=2)

    result = submit_to_gate(db, case)

    assert result["decision"] == "BLOCK"
    gate = result["gate"]
    assert gate["reason"] == "ATTEMPT_LIMIT_REACHED"
    assert gate["alternative"] == "WAIT_AND_MONITOR"
    failed = [c for c in gate["checks"] if c["result"] == "FAIL"]
    assert failed == [{"name": "attempt_limit", "result": "FAIL", "reason": "ATTEMPT_LIMIT_REACHED"}]

    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.ACTION_SELECTED
    assert case.selected_action == "WAIT_AND_MONITOR"
    events = [(log.event_type, log.from_status, log.to_status) for log in _audit(db, case)]
    assert events[-1] == ("gate.blocked", "SAFETY_CHECK", "ACTION_SELECTED")
    assert _gate_actions(db, case)[-1].allowed is False

    second = submit_to_gate(db, case)
    db.commit()
    assert second["decision"] == "ALLOW"
    assert _refresh(db, case).status == RecoveryCaseStatus.SAFETY_CHECK
    assert _refresh(db, case).selected_action == "WAIT_AND_MONITOR"
    skips = [c for c in second["gate"]["checks"] if c["name"] == "attempt_limit"]
    assert skips[0]["result"] == "SKIP"


def test_reselection_budget_exhaustion_escalates(db, make_customer, make_order, make_payment, post_event):
    case = _selected(db, _make_case(db, make_customer, make_order, make_payment, post_event), attempt_count=2)
    first = submit_to_gate(db, case)
    db.commit()
    assert first["decision"] == "BLOCK"

    case = _selected(db, case, action="RETRY_PAYMENT_LINK", attempt_count=2)

    second = submit_to_gate(db, case)
    db.commit()

    assert second["decision"] == "BLOCK"
    assert second["gate"].get("alternative") is None
    assert second["gate"]["reason"] == "ATTEMPT_LIMIT_REACHED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.ESCALATED
    events = [log.event_type for log in _audit(db, case)]
    assert events[-1] == "gate.escalated"


def test_captured_payment_escalates(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.status = PaymentStatus.CAPTURED
    db.commit()
    case = _selected(db, case)

    result = submit_to_gate(db, case)
    db.commit()

    assert result["decision"] == "ESCALATE"
    assert result["gate"]["reason"] == "PAYMENT_ALREADY_SUCCESSFUL"
    assert _refresh(db, case).status == RecoveryCaseStatus.ESCALATED
    assert _gate_actions(db, case)[-1].allowed is False


def test_pending_payment_blocks_act_action_with_alternative(
    db, make_customer, make_order, make_payment, post_event
):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.status = PaymentStatus.PENDING
    db.commit()
    case = _selected(db, case, action="RETRY_PAYMENT_LINK")

    result = submit_to_gate(db, case)
    db.commit()

    assert result["decision"] == "BLOCK"
    assert result["gate"]["reason"] == "PAYMENT_NOT_FAILED"
    assert result["gate"]["alternative"] == "WAIT_AND_MONITOR"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.ACTION_SELECTED
    assert case.selected_action == "WAIT_AND_MONITOR"


def test_monitor_action_allowed_on_pending_payment(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.status = PaymentStatus.PENDING
    db.commit()
    case = _selected(db, case, action="WAIT_AND_MONITOR")

    result = submit_to_gate(db, case)
    db.commit()

    assert result["decision"] == "ALLOW"
    assert _refresh(db, case).status == RecoveryCaseStatus.SAFETY_CHECK


def test_missing_order_identity_escalates(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.order_id = None
    db.commit()
    case = _selected(db, case)

    result = submit_to_gate(db, case)
    db.commit()

    assert result["decision"] == "ESCALATE"
    assert result["gate"]["reason"] == "MISSING_ORDER_IDENTITY"
    assert _refresh(db, case).status == RecoveryCaseStatus.ESCALATED


def test_amount_mismatch_escalates_at_gate(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.order.amount = Decimal("2500.00")
    db.commit()
    case = _selected(db, case)

    result = submit_to_gate(db, case)
    db.commit()

    assert result["decision"] == "ESCALATE"
    assert result["gate"]["reason"] == "AMOUNT_MISMATCH"


def test_conflicting_order_state_escalates(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.order.status = OrderStatus.PAID
    db.commit()
    case = _selected(db, case)

    result = submit_to_gate(db, case)
    db.commit()

    assert result["decision"] == "ESCALATE"
    assert result["gate"]["reason"] == "CONFLICTING_ORDER_STATE"


def test_duplicate_active_case_on_order_escalates(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    other = make_payment(order=payment.order, amount=Decimal("2000.00"), status=PaymentStatus.FAILED)
    db.add(
        RecoveryCase(
            payment_id=other.id,
            revenue_at_risk=Decimal("2000.00"),
            status=RecoveryCaseStatus.DIAGNOSING,
            expiry=datetime.now(timezone.utc) + timedelta(hours=24),
        )
    )
    db.commit()
    case = _selected(db, case)

    result = submit_to_gate(db, case)
    db.commit()

    assert result["decision"] == "ESCALATE"
    assert result["gate"]["reason"] == "DUPLICATE_ACTIVE_CASE"


def test_expired_case_stops_at_gate(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    case.expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    case = _selected(db, case)

    result = submit_to_gate(db, case)
    db.commit()

    assert result["decision"] == "BLOCK"
    assert result["gate"]["reason"] == "CASE_WINDOW_EXPIRED"
    assert result["gate"]["terminal"] == "STOPPED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.STOPPED
    events = [(log.event_type, log.to_status) for log in _audit(db, case)]
    assert events[-1] == ("gate.case_stopped", "STOPPED")


def test_invalid_action_escalates(db, make_customer, make_order, make_payment, post_event):
    case = _selected(db, _make_case(db, make_customer, make_order, make_payment, post_event), action=None)

    result = submit_to_gate(db, case)
    db.commit()

    assert result["decision"] == "ESCALATE"
    assert result["gate"]["reason"] == "NO_VALID_ACTION"
    assert _refresh(db, case).status == RecoveryCaseStatus.ESCALATED


def test_gate_rejects_non_submittable_states(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    result = submit_to_gate(db, case)

    assert result["decision"] == "NOOP"
    assert "ACTION_SELECTED or SAFETY_CHECK" in result["reason"]
    assert result["gate"] is None
    assert _gate_actions(db, case) == []
    events = [log.event_type for log in _audit(db, case)]
    assert "gate.submitted" not in events

    case = _refresh(db, case)
    case.status = RecoveryCaseStatus.RECOVERED
    db.commit()
    result = submit_to_gate(db, case)
    assert result["decision"] == "NOOP"
    assert "terminal" in result["reason"]


def test_run_agent_pipeline_ends_in_safety_check(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(
        db, make_customer, make_order, make_payment, post_event,
        lifetime_payments=10, lifetime_successes=9,
    )

    result = run_agent(db, case.id)

    assert result["decision"] == "SAFETY_CHECK"
    assert result["gate"]["decision"] == "ALLOW"
    assert result["gate"]["gate"]["reason"] == "ALL_CHECKS_PASSED"
    assert result["tool_calls"][-1] == {"tool_name": "safety_gate", "status": "ALLOW"}
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    assert case.selected_action == "RETRY_PAYMENT_LINK"
    assert case.score == 94

    action_names = [action.tool_name for action in case.agent_actions]
    assert action_names[-1] == "safety_gate"
    assert action_names == [
        "get_payment_status",
        "get_order_details",
        "get_customer_history",
        "submit_diagnosis",
        "calculate_recovery_score",
        "safety_gate",
    ]
    events = [(log.event_type, log.from_status, log.to_status) for log in _audit(db, case)]
    assert events == [
        ("risk.case_created", None, "DETECTED"),
        ("agent.diagnosis_started", "DETECTED", "DIAGNOSING"),
        ("agent.diagnosis_completed", "DIAGNOSING", None),
        ("agent.scored", "DIAGNOSING", "SCORED"),
        ("agent.action_selected", "SCORED", "ACTION_SELECTED"),
        ("gate.submitted", "ACTION_SELECTED", "SAFETY_CHECK"),
        ("gate.allowed", "SAFETY_CHECK", None),
    ]


def test_run_agent_blocked_action_reselects_alternative(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    case.attempt_count = 2
    db.commit()

    result = run_agent(db, case.id)

    assert result["decision"] == "SAFETY_CHECK"
    gate_calls = [call for call in result["tool_calls"] if call["tool_name"] == "safety_gate"]
    assert gate_calls == [{"tool_name": "safety_gate", "status": "BLOCK"}, {"tool_name": "safety_gate", "status": "ALLOW"}]
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    assert case.selected_action == "WAIT_AND_MONITOR"
    events = [log.event_type for log in _audit(db, case)]
    assert events.count("gate.submitted") == 2
    assert events.count("gate.blocked") == 1
    assert events.count("gate.allowed") == 1


def test_pure_evaluate_has_no_side_effects(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    case.selected_action = "RETRY_PAYMENT_LINK"
    db.commit()
    before_actions = db.query(AgentAction).filter(AgentAction.case_id == case.id).count()
    before_audit = db.query(AuditLog).filter(AuditLog.case_id == case.id).count()

    result = evaluate(db, case)

    assert result["decision"] == "ALLOW"
    assert db.query(AgentAction).filter(AgentAction.case_id == case.id).count() == before_actions
    assert db.query(AuditLog).filter(AuditLog.case_id == case.id).count() == before_audit


def test_gate_api_endpoint(client, db, make_customer, make_order, make_payment, post_event):
    response = client.post("/api/cases/999/gate/evaluate")
    assert response.status_code == 404

    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    case = _selected(db, case)

    response = client.post(f"/api/cases/{case.id}/gate/evaluate")
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "ALLOW"
    assert body["case_status"] == "SAFETY_CHECK"

    response = client.post(f"/api/cases/{case.id}/gate/evaluate")
    assert response.status_code == 200
    assert response.json()["gate"]["replay"] is True

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["agent_actions"][-1]["tool_name"] == "safety_gate"
    assert detail["agent_actions"][-1]["allowed"] is True
    assert detail["audit_logs"][-1]["event_type"] == "gate.allowed"
