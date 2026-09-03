"""Phase 6 tests: deterministic scoring, thresholds and decision policy."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from models import Payment, PaymentStatus, RecoveryCase, RecoveryCaseStatus
from services.agent import run_agent
from services.scoring import (
    WEIGHTS,
    _amount_factor,
    _failure_reason_factor,
    _method_factor,
    _prior_attempts_factor,
    _recency_factor,
    band_for_score,
    decide_action,
)


def test_band_boundaries():
    assert band_for_score(100) == "HIGH"
    assert band_for_score(80) == "HIGH"
    assert band_for_score(79) == "MEDIUM"
    assert band_for_score(35) == "MEDIUM"
    assert band_for_score(34) == "LOW"
    assert band_for_score(0) == "LOW"


def test_failure_reason_factors():
    assert _failure_reason_factor("INSUFFICIENT_FUNDS") == 1.0
    assert _failure_reason_factor("BANK_TIMEOUT") == 0.8
    assert _failure_reason_factor("NETWORK_ERROR") == 0.8
    assert _failure_reason_factor("AUTHENTICATION_FAILED") == 0.5
    assert _failure_reason_factor("CARD_DECLINED") == 0.4
    assert _failure_reason_factor("INVALID_VPA") == 0.3
    assert _failure_reason_factor("RISK_BLOCKED") == 0.0
    assert _failure_reason_factor("SOMETHING_ELSE") == 0.5
    assert _failure_reason_factor(None) == 0.5


def test_amount_factor_bands():
    assert _amount_factor(Decimal("100.00")) == 1.0
    assert _amount_factor(Decimal("500.00")) == 1.0
    assert _amount_factor(Decimal("501.00")) == 0.8
    assert _amount_factor(Decimal("2000.00")) == 0.8
    assert _amount_factor(Decimal("2001.00")) == 0.6
    assert _amount_factor(Decimal("10000.00")) == 0.6
    assert _amount_factor(Decimal("10001.00")) == 0.4
    assert _amount_factor(None) == 0.5


def test_method_factors():
    assert _method_factor("UPI") == 0.9
    assert _method_factor("CARD") == 0.8
    assert _method_factor("NETBANKING") == 0.7
    assert _method_factor("WALLET") == 0.6
    assert _method_factor("CASH") == 0.5
    assert _method_factor(None) == 0.5


def test_prior_attempts_factor():
    assert _prior_attempts_factor(0, 0) == 1.0
    assert abs(_prior_attempts_factor(2, 1) - 0.4) < 1e-9
    assert _prior_attempts_factor(5, 0) == 0.0
    assert _prior_attempts_factor(10, 3) == 0.0


def test_recency_factor():
    now = datetime.now(timezone.utc)
    assert _recency_factor(now) == 1.0
    assert abs(_recency_factor(now - timedelta(hours=12)) - 0.6) < 0.001
    assert _recency_factor(now - timedelta(hours=24)) == 0.2
    assert _recency_factor(now - timedelta(hours=72)) == 0.2


def test_weights_sum_to_100():
    assert sum(WEIGHTS.values()) == 100


def test_decision_policy_matrix():
    assert decide_action("HIGH", "RETRY_PAYMENT_LINK") == {
        "selected_action": "RETRY_PAYMENT_LINK",
        "decision": "PROCEED",
        "reason": "high band: the agent's recommended action is eligible for recovery",
    }
    medium = decide_action("MEDIUM", "RETRY_PAYMENT_LINK")
    assert medium["selected_action"] == "SEND_NOTIFICATION_ONLY"
    assert medium["decision"] == "CAUTIOUS"
    assert "downgraded" in medium["reason"]

    for band in ("HIGH", "MEDIUM"):
        kept = decide_action(band, "SEND_NOTIFICATION_ONLY")
        assert kept["selected_action"] == "SEND_NOTIFICATION_ONLY"
        waited = decide_action(band, "WAIT_AND_MONITOR")
        assert waited["selected_action"] == "WAIT_AND_MONITOR"

    low = decide_action("LOW", "RETRY_PAYMENT_LINK")
    assert low["selected_action"] is None
    assert low["decision"] == "STOP"
    assert "stop threshold" in low["reason"]

    for band in ("LOW", "MEDIUM", "HIGH"):
        escalated = decide_action(band, "ESCALATE")
        assert escalated["decision"] == "ESCALATE"
        assert escalated["selected_action"] == "ESCALATE"


def test_low_score_stops_case(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer(
        lifetime_payments=10, lifetime_successes=1, prior_recovery_attempts=5
    )
    order = make_order(customer=customer, amount=Decimal("15000.00"))
    payment = make_payment(
        order=order, amount=Decimal("15000.00"), status=PaymentStatus.PENDING, method="CARD"
    )
    post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "OTP authentication failed"}
    )
    case = db.query(RecoveryCase).one()
    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    payment.updated_at = datetime.now(timezone.utc) - timedelta(hours=24)
    db.commit()

    result = run_agent(db, case.id)

    assert result["decision"] == "STOPPED"
    assert result["score"]["score"] == 30
    assert result["score"]["band"] == "LOW"
    assert result["selection"]["decision"] == "STOP"
    assert result["selection"]["selected_action"] is None
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()
    assert case.status == RecoveryCaseStatus.STOPPED
    assert case.score == 30
    assert case.selected_action is None

    audit = [(log.event_type, log.to_status) for log in case.audit_logs]
    assert audit[-1] == ("agent.case_stopped", "STOPPED")

    rerun = run_agent(db, case.id)
    assert rerun["decision"] == "NOOP"
    assert "terminal" in rerun["reason"]


def test_case_attempt_count_reduces_score(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer(lifetime_payments=10, lifetime_successes=9)
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    case = db.query(RecoveryCase).one()
    case.attempt_count = 2
    db.commit()

    from services.agent.tools import execute_tool

    out = execute_tool(db, case, "calculate_recovery_score", {})

    assert out["score"] == 90
    prior = [factor for factor in out["factors"] if factor["name"] == "prior_attempts"][0]
    assert prior["factor"] == 0.6
    assert prior["points"] == 6.0
