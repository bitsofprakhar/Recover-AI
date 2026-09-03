"""Phase 4 tests: revenue-at-risk detection and deterministic case creation."""
from datetime import timedelta
from decimal import Decimal

from models import (
    AuditLog,
    OrderStatus,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.revenue_risk import UNCERTAIN_FAILURE_REASONS


def _cases(db):
    return db.query(RecoveryCase).order_by(RecoveryCase.id).all()


def _audits(db, case_id):
    return db.query(AuditLog).filter(AuditLog.case_id == case_id).order_by(AuditLog.id).all()


def _make_clean_failed_case(db, make_customer, make_order, make_payment, post_event, amount=Decimal("2000.00")):
    customer = make_customer()
    order = make_order(customer=customer, amount=amount)
    payment = make_payment(order=order, amount=amount, status=PaymentStatus.PENDING)
    result = post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    return customer, order, payment, result


def test_clean_failed_payment_creates_detected_case(db, make_customer, make_order, make_payment, post_event):
    customer, order, payment, result = _make_clean_failed_case(db, make_customer, make_order, make_payment, post_event)

    assert result["processing_status"] == "PROCESSED"
    risk = result["risk_evaluation"]
    assert risk["decision"] == "CASE_CREATED"
    assert risk["reason"] is None
    assert risk["revenue_at_risk"] == "2000.00"

    cases = _cases(db)
    assert len(cases) == 1
    case = cases[0]
    assert risk["case_id"] == case.id
    assert case.status == RecoveryCaseStatus.DETECTED
    assert case.payment_id == payment.id
    assert case.revenue_at_risk == Decimal("2000.00")
    assert case.attempt_count == 0
    assert case.diagnosis is None
    assert case.score is None
    assert case.selected_action is None
    assert timedelta(hours=23, minutes=59) < (case.expiry - case.created_at) <= timedelta(hours=24, minutes=1)

    audits = _audits(db, case.id)
    assert [a.event_type for a in audits] == ["risk.case_created"]
    assert audits[0].to_status == "DETECTED"
    assert audits[0].payload["revenue_at_risk"] == "2000.00"
    assert audits[0].payload["ambiguity_reason"] is None


def test_distinct_failed_event_same_payment_is_duplicate(db, make_customer, make_order, make_payment, post_event):
    customer, order, payment, _ = _make_clean_failed_case(db, make_customer, make_order, make_payment, post_event)

    result = post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Bank declined"}
    )
    assert result["processing_status"] == "PROCESSED"
    assert result["risk_evaluation"]["decision"] == "DUPLICATE_ACTIVE_CASE"
    assert result["risk_evaluation"]["case_id"] == _cases(db)[0].id
    assert len(_cases(db)) == 1


def test_failed_payment_same_order_active_case_is_duplicate(db, make_customer, make_order, make_payment, post_event):
    customer, order, payment, _ = _make_clean_failed_case(db, make_customer, make_order, make_payment, post_event)

    payment2 = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    result = post_event(
        {"payment_id": payment2.payment_id, "event": "payment.failed", "error_description": "Card declined"}
    )
    assert result["risk_evaluation"]["decision"] == "DUPLICATE_ACTIVE_CASE"
    assert len(_cases(db)) == 1
    assert _cases(db)[0].status == RecoveryCaseStatus.DETECTED


def test_pending_payment_routes_to_verification(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("500.00"))
    payment = make_payment(order=order, amount=Decimal("500.00"), status=PaymentStatus.CREATED)

    result = post_event({"payment_id": payment.payment_id, "event": "payment.authorized"})
    assert result["processing_status"] == "PROCESSED"
    assert result["risk_evaluation"]["decision"] == "PENDING_VERIFICATION"
    assert _cases(db) == []

    audit = db.query(AuditLog).filter(AuditLog.event_type == "risk.pending_verification").one()
    assert audit.payload["payment_id"] == payment.payment_id


def test_captured_payment_never_creates_case(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("750.00"))
    payment = make_payment(order=order, amount=Decimal("750.00"), status=PaymentStatus.PENDING)

    result = post_event({"payment_id": payment.payment_id, "event": "payment.captured"})
    assert result["risk_evaluation"]["decision"] == "ALREADY_SUCCESSFUL_IGNORED"
    assert _cases(db) == []
    assert payment.status == PaymentStatus.CAPTURED
    assert order.status == OrderStatus.PAID

    audit = db.query(AuditLog).filter(AuditLog.event_type == "risk.already_successful").one()
    assert audit.payload["payment_id"] == payment.payment_id


def test_missing_order_creates_escalated_case(db, post_event):
    result = post_event(
        {
            "payment_id": "pay_new_901",
            "event": "payment.failed",
            "amount_paise": 50000,
            "method": "upi",
            "error_description": "Network error",
        }
    )
    risk = result["risk_evaluation"]
    assert risk["decision"] == "CASE_ESCALATED"
    assert risk["reason"] == "AMBIGUOUS_MISSING_ORDER"
    assert risk["revenue_at_risk"] == "500.00"

    case = _cases(db)[0]
    assert case.status == RecoveryCaseStatus.ESCALATED
    assert case.revenue_at_risk == Decimal("500.00")
    assert [a.event_type for a in _audits(db, case.id)] == ["risk.case_created", "risk.case_escalated"]
    escalated = _audits(db, case.id)[1]
    assert escalated.from_status == "DETECTED"
    assert escalated.to_status == "ESCALATED"
    assert escalated.payload["reason"] == "AMBIGUOUS_MISSING_ORDER"


def test_amount_mismatch_creates_escalated_case(db, make_customer, make_order, post_event):
    customer = make_customer()
    make_order(customer=customer, amount=Decimal("100.00"))

    result = post_event(
        {
            "payment_id": "pay_new_902",
            "event": "payment.failed",
            "order_id": "order_0001",
            "amount_paise": 15000,
            "method": "card",
            "error_description": "Insufficient funds",
        }
    )
    risk = result["risk_evaluation"]
    assert risk["decision"] == "CASE_ESCALATED"
    assert risk["reason"] == "AMBIGUOUS_AMOUNT_MISMATCH"
    assert _cases(db)[0].status == RecoveryCaseStatus.ESCALATED


def test_conflicting_order_state_creates_escalated_case(db, make_customer, make_order, post_event):
    customer = make_customer()
    make_order(customer=customer, amount=Decimal("300.00"), status=OrderStatus.PAID)

    result = post_event(
        {
            "payment_id": "pay_new_903",
            "event": "payment.failed",
            "order_id": "order_0001",
            "amount_paise": 30000,
            "method": "upi",
            "error_description": "UPI transaction timed out",
        }
    )
    risk = result["risk_evaluation"]
    assert risk["decision"] == "CASE_ESCALATED"
    assert risk["reason"] == "AMBIGUOUS_CONFLICTING_STATE"
    assert _cases(db)[0].status == RecoveryCaseStatus.ESCALATED


def test_repeated_uncertain_failures_escalate_active_case(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("1200.00"))
    p1 = make_payment(order=order, amount=Decimal("1200.00"), status=PaymentStatus.PENDING)
    p2 = make_payment(order=order, amount=Decimal("1200.00"), status=PaymentStatus.PENDING)
    p3 = make_payment(order=order, amount=Decimal("1200.00"), status=PaymentStatus.PENDING)

    r1 = post_event({"payment_id": p1.payment_id, "event": "payment.failed", "error_description": "Network error"})
    assert r1["risk_evaluation"]["decision"] == "CASE_CREATED"

    r2 = post_event({"payment_id": p2.payment_id, "event": "payment.failed", "error_description": "Request timed out"})
    assert r2["risk_evaluation"]["decision"] == "DUPLICATE_ACTIVE_CASE"

    r3 = post_event({"payment_id": p3.payment_id, "event": "payment.failed", "error_description": "Network error"})
    assert r3["risk_evaluation"]["decision"] == "CASE_ESCALATED"
    assert r3["risk_evaluation"]["reason"] == "AMBIGUOUS_REPEATED_UNCERTAIN"

    cases = _cases(db)
    assert len(cases) == 1
    assert cases[0].status == RecoveryCaseStatus.ESCALATED
    assert [a.event_type for a in _audits(db, cases[0].id)] == ["risk.case_created", "risk.case_duplicate", "risk.case_escalated"]


def test_repeated_uncertain_failures_at_creation_escalate(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("900.00"))
    make_payment(order=order, amount=Decimal("900.00"), status=PaymentStatus.FAILED, failure_reason="NETWORK_ERROR")
    make_payment(order=order, amount=Decimal("900.00"), status=PaymentStatus.FAILED, failure_reason="BANK_TIMEOUT")

    result = post_event(
        {
            "payment_id": "pay_new_904",
            "event": "payment.failed",
            "order_id": order.order_id,
            "amount_paise": 90000,
            "method": "upi",
            "error_description": "Network connectivity issue",
        }
    )
    risk = result["risk_evaluation"]
    assert risk["decision"] == "CASE_ESCALATED"
    assert risk["reason"] == "AMBIGUOUS_REPEATED_UNCERTAIN"
    assert _cases(db)[0].status == RecoveryCaseStatus.ESCALATED


def test_new_case_after_terminal_case_allowed(db, make_customer, make_order, make_payment, post_event):
    customer, order, payment, _ = _make_clean_failed_case(db, make_customer, make_order, make_payment, post_event)
    case = _cases(db)[0]
    case.status = RecoveryCaseStatus.NOT_RECOVERED
    db.commit()

    payment2 = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    result = post_event(
        {"payment_id": payment2.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    assert result["risk_evaluation"]["decision"] == "CASE_CREATED"

    cases = _cases(db)
    assert len(cases) == 2
    assert cases[0].status == RecoveryCaseStatus.NOT_RECOVERED
    assert cases[1].status == RecoveryCaseStatus.DETECTED
    assert cases[1].payment_id == payment2.id


def test_event_level_duplicate_has_no_risk_evaluation(db, make_customer, make_order, make_payment):
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("400.00"))
    payment = make_payment(order=order, amount=Decimal("400.00"), status=PaymentStatus.PENDING)

    from services.event_intake import build_envelope, process_envelope

    spec = {
        "payment_id": payment.payment_id,
        "event": "payment.failed",
        "error_description": "Insufficient funds",
        "created_at": 1756600500,
    }
    first = process_envelope(db, build_envelope(db, dict(spec)), "SYNTHETIC")
    second = process_envelope(db, build_envelope(db, dict(spec)), "SYNTHETIC")

    assert first["processing_status"] == "PROCESSED"
    assert first["risk_evaluation"]["decision"] == "CASE_CREATED"
    assert second["processing_status"] == "DUPLICATE"
    assert "risk_evaluation" not in second
    assert len(_cases(db)) == 1


def test_http_synthetic_event_response_includes_risk_evaluation(client, db, make_customer, make_order, make_payment):
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("1500.00"))
    payment = make_payment(order=order, amount=Decimal("1500.00"), status=PaymentStatus.PENDING)

    response = client.post(
        "/api/events/synthetic",
        json={
            "payment_id": payment.payment_id,
            "event": "payment.failed",
            "error_description": "Insufficient funds",
            "created_at": 1756600600,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["processing_status"] == "PROCESSED"
    assert body["risk_evaluation"]["decision"] == "CASE_CREATED"
    assert body["risk_evaluation"]["revenue_at_risk"] == "1500.00"

    cases = _cases(db)
    assert len(cases) == 1
    assert cases[0].status == RecoveryCaseStatus.DETECTED
    assert cases[0].revenue_at_risk == Decimal("1500.00")
