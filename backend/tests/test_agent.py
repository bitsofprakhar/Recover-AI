"""Phase 5 tests: agent context, controlled tools, GLM integration, orchestration."""
import json
from decimal import Decimal

import pytest

from config import settings
from models import (
    AgentAction,
    AuditLog,
    OrderStatus,
    Payment,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.agent import llm
from services.agent import run_agent
from services.agent.orchestrator import CaseNotFoundError
from services.agent.tools import APPROVED_ACTIONS, execute_tool
from services.case_lifecycle import IllegalTransitionError, transition


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


def _actions(db, case):
    return db.query(AgentAction).filter(AgentAction.case_id == case.id).order_by(AgentAction.id).all()


def _audits(db, case):
    return db.query(AuditLog).filter(AuditLog.case_id == case.id).order_by(AuditLog.id).all()


def _diagnosis(case):
    return json.loads(case.diagnosis)


def _refresh(db, case):
    db.expire_all()
    return db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()


def test_agent_run_diagnoses_selects_and_clears_gate(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    result = run_agent(db, case.id)

    assert result["decision"] == "SAFETY_CHECK"
    assert result["case_status"] == "SAFETY_CHECK"
    assert result["context_assessment"] == {"complete": True, "ambiguities": []}
    assert [call["tool_name"] for call in result["tool_calls"]] == [
        "get_payment_status",
        "get_order_details",
        "get_customer_history",
        "calculate_recovery_score",
        "safety_gate",
    ]

    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    assert case.score == 94
    assert case.selected_action == "RETRY_PAYMENT_LINK"
    diagnosis = _diagnosis(case)
    assert diagnosis["recommended_action"] in APPROVED_ACTIONS
    assert diagnosis["reasoning_source"] == "rule_based_fallback"
    assert diagnosis["failure_analysis"]
    assert diagnosis["confidence"] in ("HIGH", "MEDIUM", "LOW")

    assert result["score"]["score"] == 94
    assert result["score"]["band"] == "HIGH"
    assert result["selection"]["selected_action"] == "RETRY_PAYMENT_LINK"
    assert result["selection"]["decision"] == "PROCEED"
    assert result["gate"]["decision"] == "ALLOW"
    assert result["gate"]["gate"]["reason"] == "ALL_CHECKS_PASSED"

    action_names = [action.tool_name for action in _actions(db, case)]
    assert action_names == [
        "get_payment_status",
        "get_order_details",
        "get_customer_history",
        "submit_diagnosis",
        "calculate_recovery_score",
        "safety_gate",
    ]
    gate_rows = [action for action in _actions(db, case) if action.tool_name == "safety_gate"]
    assert gate_rows[0].allowed is True
    read_actions = [
        action
        for action in _actions(db, case)
        if action.tool_name not in ("submit_diagnosis", "safety_gate")
    ]
    assert all(action.allowed is None for action in read_actions)
    submit = [action for action in _actions(db, case) if action.tool_name == "submit_diagnosis"][0]
    assert submit.allowed is True
    assert submit.output["recommended_action"] == diagnosis["recommended_action"]

    audit = _audits(db, case)
    assert [(log.event_type, log.from_status, log.to_status) for log in audit] == [
        ("risk.case_created", None, "DETECTED"),
        ("agent.diagnosis_started", "DETECTED", "DIAGNOSING"),
        ("agent.diagnosis_completed", "DIAGNOSING", None),
        ("agent.scored", "DIAGNOSING", "SCORED"),
        ("agent.action_selected", "SCORED", "ACTION_SELECTED"),
        ("gate.submitted", "ACTION_SELECTED", "SAFETY_CHECK"),
        ("gate.allowed", "SAFETY_CHECK", None),
    ]


def test_fallback_recommendation_follows_customer_history(db, make_customer, make_order, make_payment, post_event):
    strong = _make_case(
        db, make_customer, make_order, make_payment, post_event,
        lifetime_payments=10, lifetime_successes=9,
    )
    result = run_agent(db, strong.id)
    assert result["diagnosis"]["recommended_action"] == "RETRY_PAYMENT_LINK"
    assert result["diagnosis"]["confidence"] == "HIGH"
    assert result["score"]["score"] == 94
    assert result["score"]["band"] == "HIGH"
    assert result["selection"]["selected_action"] == "RETRY_PAYMENT_LINK"
    assert result["gate"]["decision"] == "ALLOW"
    assert _refresh(db, strong).status == RecoveryCaseStatus.SAFETY_CHECK

    medium = _make_case(
        db, make_customer, make_order, make_payment, post_event,
        lifetime_payments=10, lifetime_successes=4,
    )
    result = run_agent(db, medium.id)
    assert result["diagnosis"]["recommended_action"] == "SEND_NOTIFICATION_ONLY"
    assert result["score"]["score"] == 79
    assert result["score"]["band"] == "MEDIUM"
    assert result["selection"]["selected_action"] == "SEND_NOTIFICATION_ONLY"
    assert result["selection"]["decision"] == "CAUTIOUS"
    assert result["gate"]["decision"] == "ALLOW"
    assert _refresh(db, medium).status == RecoveryCaseStatus.SAFETY_CHECK

    weak = _make_case(
        db, make_customer, make_order, make_payment, post_event,
        lifetime_payments=10, lifetime_successes=2,
    )
    result = run_agent(db, weak.id)
    assert result["diagnosis"]["recommended_action"] == "WAIT_AND_MONITOR"
    assert result["score"]["score"] == 73
    assert result["selection"]["selected_action"] == "WAIT_AND_MONITOR"
    assert result["gate"]["decision"] == "ALLOW"
    assert _refresh(db, weak).status == RecoveryCaseStatus.SAFETY_CHECK


def test_risk_blocked_failure_escalates_after_scoring(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer(lifetime_payments=10, lifetime_successes=9)
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Blocked due to risk"}
    )
    case = db.query(RecoveryCase).one()

    result = run_agent(db, case.id)

    assert result["decision"] == "ESCALATED"
    assert result["diagnosis"]["recommended_action"] == "ESCALATE"
    assert result["diagnosis"]["escalate_reason"]
    assert result["gate"] is None
    assert result["score"]["score"] == 69
    assert result["selection"]["decision"] == "ESCALATE"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.ESCALATED
    assert case.score == 69
    assert case.selected_action == "ESCALATE"
    audit = _audits(db, case)
    assert [(log.event_type, log.from_status, log.to_status) for log in audit] == [
        ("risk.case_created", None, "DETECTED"),
        ("agent.diagnosis_started", "DETECTED", "DIAGNOSING"),
        ("agent.diagnosis_completed", "DIAGNOSING", None),
        ("agent.scored", "DIAGNOSING", "SCORED"),
        ("agent.case_escalated", "SCORED", "ESCALATED"),
    ]


def test_conflicting_context_escalates_case(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.status = PaymentStatus.CAPTURED
    db.commit()

    result = run_agent(db, case.id)

    assert result["decision"] == "ESCALATED"
    assert result["context_assessment"]["ambiguities"] == ["PAYMENT_STATE_CHANGED"]
    assert result["diagnosis"]["recommended_action"] == "ESCALATE"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.ESCALATED
    assert _diagnosis(case)["escalate_reason"] == "PAYMENT_STATE_CHANGED"
    audit = _audits(db, case)
    assert (audit[-1].event_type, audit[-1].from_status, audit[-1].to_status) == (
        "agent.case_escalated",
        "DIAGNOSING",
        "ESCALATED",
    )


def test_amount_mismatch_drift_escalates_case(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.order.amount = Decimal("2500.00")
    db.commit()

    result = run_agent(db, case.id)

    assert result["decision"] == "ESCALATED"
    assert "AMOUNT_MISMATCH" in result["context_assessment"]["ambiguities"]
    assert _refresh(db, case).status == RecoveryCaseStatus.ESCALATED


def test_terminal_case_is_noop(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    case.status = RecoveryCaseStatus.NOT_RECOVERED
    db.commit()

    result = run_agent(db, case.id)

    assert result["decision"] == "NOOP"
    assert "terminal" in result["reason"]
    assert _actions(db, case) == []


def test_non_diagnosable_status_is_noop(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    case.status = RecoveryCaseStatus.SCORED
    db.commit()

    result = run_agent(db, case.id)

    assert result["decision"] == "NOOP"
    assert "DETECTED or DIAGNOSING" in result["reason"]
    assert _actions(db, case) == []


def test_act_tools_require_gate_authorization(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    out = execute_tool(db, case, "create_recovery_payment", {"amount": 2000})
    assert out["status"] == "BLOCKED"
    assert out["executed"] is False
    assert out["reason"] == "GATE_AUTHORIZATION_REQUIRED"
    row = _actions(db, case)[-1]
    assert row.tool_name == "create_recovery_payment"
    assert row.input == {"amount": 2000}
    assert row.allowed is False

    out = execute_tool(db, case, "send_recovery_notification", {"channel": "EMAIL"})
    assert out["status"] == "BLOCKED"
    assert out["executed"] is False

    out = execute_tool(db, case, "send_recovery_notification", {"channel": "PAGER"})
    assert out["status"] == "ERROR"
    assert out["error"] == "INVALID_ARGUMENTS"
    assert _actions(db, case)[-1].allowed is False

    out = execute_tool(db, case, "send_recovery_notification", {})
    assert out["status"] == "ERROR"


def test_act_tool_on_terminal_case_blocked(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    case.status = RecoveryCaseStatus.STOPPED
    db.commit()

    out = execute_tool(db, case, "create_recovery_payment", {"amount": 100})
    assert out["status"] == "BLOCKED"
    assert out["reason"] == "TERMINAL_CASE"


def test_unknown_tool_is_rejected(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    out = execute_tool(db, case, "drop_database", {})
    assert out["status"] == "ERROR"
    assert out["error"] == "UNKNOWN_TOOL"
    row = _actions(db, case)[-1]
    assert row.tool_name == "drop_database"
    assert row.allowed is False


def test_log_action_tool_records_audit_note(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    out = execute_tool(db, case, "log_action", {"note": "diagnosis reviewed"})
    assert out["status"] == "OK"
    assert out["recorded"] is True
    assert _actions(db, case)[-1].allowed is True
    note = db.query(AuditLog).filter(AuditLog.event_type == "agent.note").one()
    assert note.case_id == case.id
    assert note.payload["note"] == "diagnosis reviewed"

    out = execute_tool(db, case, "log_action", {})
    assert out["status"] == "ERROR"
    assert out["error"] == "INVALID_ARGUMENTS"


def test_check_recovery_result_returns_current_state(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    out = execute_tool(db, case, "check_recovery_result", {})

    assert out["status"] == "OK"
    assert out["payment_status"] == "FAILED"
    assert out["order_status"] == OrderStatus.ATTEMPTED.value
    assert out["recovery_action_executed"] is False


def test_calculate_recovery_score_returns_explainable_score(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    out = execute_tool(db, case, "calculate_recovery_score", {})

    assert out["status"] == "OK"
    assert out["score"] == 94
    assert out["band"] == "HIGH"
    assert out["thresholds"] == {"high": settings.score_high_threshold, "stop": settings.score_stop_threshold}
    assert [factor["name"] for factor in out["factors"]] == [
        "failure_reason",
        "customer_success_rate",
        "method",
        "amount",
        "prior_attempts",
        "recency",
    ]
    assert sum(factor["weight"] for factor in out["factors"]) == 100
    assert round(sum(factor["points"] for factor in out["factors"])) == out["score"]
    assert out["inputs"]["failure_reason"] == "INSUFFICIENT_FUNDS"
    assert out["inputs"]["method"] == "UPI"
    assert out["inputs"]["customer_success_rate"] == 0.9
    assert out["inputs"]["case_attempt_count"] == 0
    assert "explainable" in out["note"]


def test_customer_history_masks_pii(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer(email="deep.customer@example.com", phone="+919812345678")
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    post_event({"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"})
    case = db.query(RecoveryCase).one()

    out = execute_tool(db, case, "get_customer_history", {})

    assert out["found"] is True
    assert out["email_masked"].startswith("de")
    assert "@" in out["email_masked"]
    assert out["email_masked"].count("*") >= 2
    assert "deep.customer@example.com" not in json.dumps(out)
    assert out["phone_masked"].endswith("5678")
    assert "+919812345678" not in json.dumps(out)
    assert out["success_rate"] == 0.9
    assert out["order_payment_history"][0]["payment_id"] == payment.payment_id


def test_llm_mode_uses_structured_tool_calls(db, monkeypatch, make_customer, make_order, make_payment, post_event):
    case = _make_case(
        db, make_customer, make_order, make_payment, post_event,
        lifetime_payments=10, lifetime_successes=9,
    )
    state = {"turn": 0}

    def fake_chat(messages, tools, tool_choice="auto"):
        state["turn"] += 1
        if state["turn"] == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "get_payment_status", "arguments": "{}"}}
                ],
            }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "submit_diagnosis",
                        "arguments": json.dumps(
                            {
                                "failure_analysis": "UPI payment failed with INSUFFICIENT_FUNDS.",
                                "customer_assessment": "Reliable customer, 90% success rate.",
                                "recovery_strategy": "Offer a recovery payment link.",
                                "recommended_action": "RETRY_PAYMENT_LINK",
                                "recommendation_reasoning": "Retryable failure with strong history.",
                                "confidence": "HIGH",
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "chat", fake_chat)

    result = run_agent(db, case.id)

    assert result["decision"] == "SAFETY_CHECK"
    assert result["diagnosis"]["recommended_action"] == "RETRY_PAYMENT_LINK"
    assert result["diagnosis"]["reasoning_source"] == settings.agent_llm_model
    assert "fallback_note" not in result["diagnosis"]
    assert result["score"]["score"] == 94
    assert result["selection"]["selected_action"] == "RETRY_PAYMENT_LINK"
    assert result["gate"]["decision"] == "ALLOW"

    action_names = [action.tool_name for action in _actions(db, case)]
    assert action_names.count("get_payment_status") == 2
    assert action_names == [
        "get_payment_status",
        "get_order_details",
        "get_customer_history",
        "get_payment_status",
        "submit_diagnosis",
        "calculate_recovery_score",
        "safety_gate",
    ]
    llm_call = result["tool_calls"][-3]
    assert llm_call == {"tool_name": "get_payment_status", "status": "OK"}
    assert result["tool_calls"][-2] == {"tool_name": "calculate_recovery_score", "status": "OK"}
    assert result["tool_calls"][-1] == {"tool_name": "safety_gate", "status": "ALLOW"}
    assert _refresh(db, case).status == RecoveryCaseStatus.SAFETY_CHECK


def test_llm_invalid_recommendation_falls_back(db, monkeypatch, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    def fake_chat(messages, tools, tool_choice="auto"):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "submit_diagnosis",
                        "arguments": json.dumps(
                            {
                                "failure_analysis": "analysis",
                                "recommended_action": "PHONE_THE_CUSTOMER",
                                "recommendation_reasoning": "made-up action",
                                "confidence": "HIGH",
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "chat", fake_chat)

    result = run_agent(db, case.id)

    assert result["decision"] == "SAFETY_CHECK"
    assert result["diagnosis"]["recommended_action"] in APPROVED_ACTIONS
    assert result["diagnosis"]["reasoning_source"] == "rule_based_fallback"
    assert "failed backend validation" in result["diagnosis"]["fallback_note"]
    assert result["score"]["score"] == 94


def test_llm_error_falls_back(db, monkeypatch, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    def fake_chat(messages, tools, tool_choice="auto"):
        raise llm.LLMError("transport error: connection refused")

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "chat", fake_chat)

    result = run_agent(db, case.id)

    assert result["decision"] == "SAFETY_CHECK"
    assert result["diagnosis"]["reasoning_source"] == "rule_based_fallback"
    assert "GLM call failed" in result["diagnosis"]["fallback_note"]
    assert result["diagnosis"]["recommended_action"] in APPROVED_ACTIONS


def test_case_not_found_raises(db):
    with pytest.raises(CaseNotFoundError):
        run_agent(db, 99999)


def test_state_machine_rejects_illegal_transitions(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    with pytest.raises(IllegalTransitionError):
        transition(db, case, RecoveryCaseStatus.ACTION_EXECUTED, "test")

    case.status = RecoveryCaseStatus.RECOVERED
    db.commit()

    with pytest.raises(IllegalTransitionError):
        transition(db, case, RecoveryCaseStatus.DIAGNOSING, "test")


def test_api_case_endpoints(client, db, make_customer, make_order, make_payment, post_event):
    response = client.post("/api/cases/999/agent/run")
    assert response.status_code == 404

    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    response = client.post(f"/api/cases/{case.id}/agent/run")
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "SAFETY_CHECK"
    assert body["diagnosis"]["recommended_action"] in APPROVED_ACTIONS
    assert body["score"]["score"] == 94
    assert body["selection"]["selected_action"] == "RETRY_PAYMENT_LINK"
    assert body["gate"]["decision"] == "ALLOW"

    response = client.get("/api/cases")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["id"] == case.id
    assert item["status"] == "SAFETY_CHECK"
    assert item["revenue_at_risk"] == "2000.00"
    assert item["score"] == 94
    assert item["selected_action"] == "RETRY_PAYMENT_LINK"
    assert item["diagnosis"]["recommended_action"] in APPROVED_ACTIONS
    assert item["payment"]["failure_reason"] == "INSUFFICIENT_FUNDS"

    response = client.get("/api/cases", params={"status": "SAFETY_CHECK"})
    assert response.json()["total"] == 1
    response = client.get("/api/cases", params={"status": "BOGUS"})
    assert response.status_code == 422

    response = client.get(f"/api/cases/{case.id}")
    assert response.status_code == 200
    detail = response.json()
    assert [action["tool_name"] for action in detail["agent_actions"]] == [
        "get_payment_status",
        "get_order_details",
        "get_customer_history",
        "submit_diagnosis",
        "calculate_recovery_score",
        "safety_gate",
    ]
    assert detail["agent_actions"][-1]["allowed"] is True
    assert [log["event_type"] for log in detail["audit_logs"]] == [
        "risk.case_created",
        "agent.diagnosis_started",
        "agent.diagnosis_completed",
        "agent.scored",
        "agent.action_selected",
        "gate.submitted",
        "gate.allowed",
    ]

    response = client.get("/api/cases/999")
    assert response.status_code == 404
