"""Phase 12 tests: full-loop validation under normal, blocked and ambiguous scenarios.

Each scenario drives the real pipeline end to end - event intake, risk
evaluation, agent, gate, executor, outcome, verification - and asserts the
final case state plus the audit evidence, exactly as README Section "Phase 12"
defines them.
"""
from decimal import Decimal

from models import (
    AuditLog,
    BackgroundJob,
    OrderStatus,
    Payment,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.action_executor import execute_action
from services.agent import run_agent
from services.jobs import run_due_jobs
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


def _refresh(db, case):
    db.expire_all()
    return db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()


def _audits(db, case):
    return db.query(AuditLog).filter(AuditLog.case_id == case.id).order_by(AuditLog.id).all()


def _events_of(db, case):
    return [log.event_type for log in _audits(db, case)]


def _to_safety_check(db, case):
    run_agent(db, case.id)
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    return case


def test_scenario_successful_recovery_full_loop(db, make_customer, make_order, make_payment, post_event):
    """Normal scenario: detected -> ... -> executed -> SUCCESS -> verified RECOVERED with attributed revenue."""
    case = _to_safety_check(db, _make_case(db, make_customer, make_order, make_payment, post_event))

    execute_action(db, case)
    simulate_outcome(db, _refresh(db, case), "SUCCESS")
    result = verify_outcome(db, _refresh(db, case))

    assert result["decision"] == "RECOVERED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.RECOVERED
    assert case.recovered_payment_id == f"pay_rec_{case.id}_1"
    assert case.recovered_amount == Decimal("2000.00")
    events = _events_of(db, case)
    for expected in (
        "risk.case_created",
        "agent.diagnosis_started",
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
    recovered = [log for log in _audits(db, case) if log.event_type == "verification.recovered"][0]
    assert recovered.to_status == "RECOVERED"
    assert recovered.payload["attribution_checks"]["after_approved_action"] is True
    assert recovered.payload["simulated"] is True


def test_scenario_failed_recovery_after_retry_limit(db, make_customer, make_order, make_payment, post_event):
    """Blocked scenario: two failed attempts -> NOT_RECOVERED; no third attempt is possible."""
    case = _to_safety_check(db, _make_case(db, make_customer, make_order, make_payment, post_event))

    execute_action(db, case)
    simulate_outcome(db, _refresh(db, case), "FAILED")
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.DIAGNOSING
    assert case.attempt_count == 1

    run_due_jobs(db)
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    execute_action(db, case)
    assert _refresh(db, case).attempt_count == 2
    simulate_outcome(db, _refresh(db, case), "FAILED")

    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.NOT_RECOVERED
    assert case.recovered_payment_id is None
    events = _events_of(db, case)
    assert events.count("outcome.retry") == 1
    assert "outcome.not_recovered" in events
    executed = [log for log in _audits(db, case) if log.event_type == "action.executed"]
    assert len(executed) == 2
    assert db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one().status not in (
        RecoveryCaseStatus.DIAGNOSING,
    )
    assert (
        db.query(BackgroundJob)
        .filter(BackgroundJob.job_key == f"agent:{case.id}:2")
        .one_or_none()
        is None
    ), "no agent job may be scheduled past the attempt limit"


def test_scenario_pending_payment_becomes_successful_before_recovery_action(
    db, make_customer, make_order, make_payment, post_event
):
    """Blocked scenario: the customer pays before the agent acts - verification-first, never a case."""
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)

    pending = post_event({"payment_id": payment.payment_id, "event": "payment.pending"})
    assert pending["risk_evaluation"]["decision"] == "PENDING_VERIFICATION"
    assert db.query(RecoveryCase).count() == 0

    captured = post_event({"payment_id": payment.payment_id, "event": "payment.captured"})
    assert captured["risk_evaluation"]["decision"] == "ALREADY_SUCCESSFUL_IGNORED"
    assert db.query(RecoveryCase).count() == 0
    assert db.query(Payment).filter(Payment.id == payment.id).one().status == PaymentStatus.CAPTURED


def test_scenario_customer_pays_before_execution_escalates(db, make_customer, make_order, make_payment, post_event):
    """Blocked scenario: the customer completes a new payment on the order after case creation but before
    execution - the gate's execution-time re-check sees the PAID order and escalates, nothing executes."""
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment_one = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    post_event(
        {"payment_id": payment_one.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    case = db.query(RecoveryCase).one()
    case = _to_safety_check(db, case)
    assert case.selected_action == "RETRY_PAYMENT_LINK"

    payment_two = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    post_event({"payment_id": payment_two.payment_id, "event": "payment.captured"})
    db.expire_all()
    assert db.query(Payment).filter(Payment.id == payment_two.id).one().status == PaymentStatus.CAPTURED

    result = execute_action(db, _refresh(db, case))

    assert result["decision"] == "ESCALATED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.ESCALATED
    assert case.attempt_count == 0
    assert (
        db.query(Payment).filter(Payment.payment_id == f"pay_rec_{case.id}_1").one_or_none() is None
    ), "nothing may execute when the gate re-check fails"
    assert "gate.escalated" in _events_of(db, case)
    assert "CONFLICTING_ORDER_STATE" in result["reason"]


def test_scenario_duplicate_webhook_event_is_idempotent(db, make_customer, make_order, make_payment, post_event):
    """Duplicate delivery of the same webhook event: DUPLICATE, zero double effects."""
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    spec = {
        "payment_id": payment.payment_id,
        "event": "payment.failed",
        "error_description": "Insufficient funds",
        "created_at": 1756600001,
    }

    first = post_event(spec)
    second = post_event(spec)

    assert first["processing_status"] == "PROCESSED"
    assert second["processing_status"] == "DUPLICATE"
    assert second["event_id"] == first["event_id"]
    from models import PaymentEvent

    assert db.query(PaymentEvent).filter(PaymentEvent.event_id == first["event_id"]).count() == 1
    assert db.query(RecoveryCase).count() == 1
    risk_audits = [log for log in db.query(AuditLog).all() if log.event_type == "risk.case_created"]
    assert len(risk_audits) == 1


def test_scenario_amount_mismatch_escalates_at_creation(db, make_customer, make_order, make_payment, post_event):
    """Ambiguous scenario: gateway amount != authoritative order amount -> born ESCALATED, no recovery action."""
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("1500.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)

    result = post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )

    assert result["risk_evaluation"]["decision"] == "CASE_ESCALATED"
    assert result["risk_evaluation"]["reason"] == "AMBIGUOUS_AMOUNT_MISMATCH"
    case = db.query(RecoveryCase).one()
    assert case.status == RecoveryCaseStatus.ESCALATED
    assert case.selected_action is None
    assert db.query(BackgroundJob).filter(BackgroundJob.name == "run_agent").count() == 0
    events = _events_of(db, case)
    assert "risk.case_created" in events
    assert "risk.case_escalated" in events


def test_scenario_conflicting_order_state_escalates(db, make_customer, make_order, make_payment, post_event):
    """Ambiguous scenario: order already PAID while the payment fails -> born ESCALATED."""
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("2000.00"), status=OrderStatus.PAID)
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)

    result = post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )

    assert result["risk_evaluation"]["decision"] == "CASE_ESCALATED"
    assert result["risk_evaluation"]["reason"] == "AMBIGUOUS_CONFLICTING_STATE"
    assert db.query(RecoveryCase).one().status == RecoveryCaseStatus.ESCALATED


def test_scenario_repeated_uncertain_failures_escalate(db, make_customer, make_order, make_payment, post_event):
    """Ambiguous scenario: repeated NETWORK_ERROR failures on one order -> escalation, never a recovery action."""
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("2000.00"))

    for reason in ("Network error", "Bank timeout", "Network error"):
        payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
        post_event({"payment_id": payment.payment_id, "event": "payment.failed", "error_description": reason})

    cases = db.query(RecoveryCase).order_by(RecoveryCase.id).all()
    escalated = [case for case in cases if case.status == RecoveryCaseStatus.ESCALATED]
    assert len(escalated) >= 1
    assert all("risk.case_escalated" in _events_of(db, case) for case in escalated)

    run_due_jobs(db, force=True)

    db.expire_all()
    for case in db.query(RecoveryCase).all():
        assert case.status == RecoveryCaseStatus.ESCALATED, "ambiguous cases never reach recovery actions"
    assert (
        db.query(Payment).filter(Payment.payment_id.like("pay_rec_%")).count() == 0
    ), "no simulated recovery payment may exist for ambiguous cases"


def test_scenario_duplicate_active_case_on_order_is_blocked(db, make_customer, make_order, make_payment, post_event):
    """Blocked scenario: a second failure on an order with an active case never creates a second case."""
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment_one = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    payment_two = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)

    first = post_event(
        {"payment_id": payment_one.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    second = post_event(
        {"payment_id": payment_two.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )

    assert first["risk_evaluation"]["decision"] == "CASE_CREATED"
    assert second["risk_evaluation"]["decision"] == "DUPLICATE_ACTIVE_CASE"
    assert db.query(RecoveryCase).count() == 1
    assert "risk.case_duplicate" in _events_of(db, db.query(RecoveryCase).one())


def test_scenario_duplicate_recovery_action_execution_is_blocked(db, make_customer, make_order, make_payment, post_event):
    """Blocked scenario: re-executing a waiting case replays idempotently - no second payment, link or attempt."""
    case = _to_safety_check(db, _make_case(db, make_customer, make_order, make_payment, post_event))

    first = execute_action(db, case)
    second = execute_action(db, _refresh(db, case))

    assert first["decision"] == "WAITING_FOR_RESULT"
    assert second["decision"] == "WAITING_FOR_RESULT"
    assert second["execution"]["replay"] is True
    assert second["execution"]["idempotency_key"] == first["execution"]["idempotency_key"]
    case = _refresh(db, case)
    assert case.attempt_count == 1
    assert (
        db.query(Payment).filter(Payment.payment_id == f"pay_rec_{case.id}_1").count() == 1
    ), "exactly one simulated recovery payment"
    executed = [log for log in _audits(db, case) if log.event_type == "action.executed"]
    assert len(executed) == 1
    assert (
        db.query(BackgroundJob)
        .filter(BackgroundJob.job_key == f"verify:{case.id}:1:executed")
        .one_or_none()
        is not None
    )


def test_scenario_unauthorized_action_tool_call_is_blocked(db, make_customer, make_order, make_payment, post_event):
    """Blocked scenario: the LLM (or any caller) cannot execute payment tools without the executor's authorization."""
    from services.agent.tools import execute_tool

    case = _to_safety_check(db, _make_case(db, make_customer, make_order, make_payment, post_event))

    out = execute_tool(db, case, "create_recovery_payment", {}, authorized=False)

    assert out["status"] == "BLOCKED"
    assert out["executed"] is False
    assert out["reason"] == "GATE_AUTHORIZATION_REQUIRED"
    assert (
        db.query(Payment).filter(Payment.payment_id == f"pay_rec_{case.id}_1").one_or_none() is None
    ), "the blocked call must not create a payment"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    assert case.attempt_count == 0


def test_scenario_expired_window_stops_case_at_gate(db, make_customer, make_order, make_payment, post_event):
    """Blocked scenario: the 24-hour window expires before execution - the gate stops the case (rule 16)."""
    from datetime import datetime, timedelta, timezone

    case = _to_safety_check(db, _make_case(db, make_customer, make_order, make_payment, post_event))
    case.expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    result = execute_action(db, _refresh(db, case))

    assert result["decision"] == "STOPPED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.STOPPED
    assert case.attempt_count == 0
    events = _events_of(db, case)
    assert "gate.case_stopped" in events
    assert "action.executed" not in events


def test_scenario_full_autonomous_loop_through_jobs(db, make_customer, make_order, make_payment, post_event):
    """The Phase 12 definition of done: one complete verified recovery with zero manual pipeline calls after creation."""
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    run_due_jobs(db)  # agent runs
    execute_action(db, _refresh(db, case))  # the one deliberate intervention point
    simulate_outcome(db, _refresh(db, case), "SUCCESS")  # scripted simulated outcome
    run_due_jobs(db, force=True)  # scheduler drains: outcome verification + delayed checks

    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.RECOVERED
    assert case.recovered_amount == Decimal("2000.00")
    metrics_events = _events_of(db, case)
    assert metrics_events[-1] == "verification.recovered"
