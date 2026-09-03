"""Event intake: raw event storage, normalization, idempotent processing and audit.

Pipeline: store raw event -> validate structure (malformed events are rejected
with HTTP 422 and not stored) -> normalize (paise to rupees, status mapping,
failure-reason taxonomy) -> semantic checks (unsupported event type, unmappable
status, event/status mismatch, unknown explicit order reference are stored as
REJECTED) -> idempotency (derived event_id, duplicates return DUPLICATE without
reprocessing) -> payment upsert under the intake state machine (CAPTURED and
FAILED are terminal) -> order status sync -> audit log entries ->
revenue-at-risk evaluation (Phase 4 deterministic case creation rules).

A payment-creating event that explicitly references an order_id unknown to the
merchant data is REJECTED (UNKNOWN_ORDER_REFERENCE): the reference contradicts
our records and is never silently dropped. An event carrying no order reference
at all is different: the payment is created orderless and the Phase 4 ambiguity
trigger escalates it (AMBIGUOUS_MISSING_ORDER) for human review.

Processing modes (reused by every caller; no duplicated logic):

- mode="autonomous" (default): the complete pipeline - payment upsert, order
  sync, risk evaluation with creation-time ambiguity escalation and the
  scheduled background agent job. Used by webhooks, replay, the demo API,
  the evaluation and every internal caller.
- mode="manual": explicit test-data entry (POST /api/events/synthetic).
  Persists the payment (an unresolvable order reference is kept in gateway
  metadata instead of rejecting the event), persists the event, creates the
  recovery case in DETECTED - the first valid non-terminal actionable state -
  and STOPS. No creation-time escalation and no agent job: every pipeline
  decision (diagnosis, escalation, scoring, gate, execution) is deferred to
  the explicit manual workflow endpoints.
"""
import hashlib
import hmac
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Order, OrderStatus, Payment, PaymentEvent, PaymentStatus
from services import revenue_risk
from services.audit import record

SUPPORTED_EVENTS = {
    "payment.captured": PaymentStatus.CAPTURED,
    "payment.failed": PaymentStatus.FAILED,
    "payment.authorized": PaymentStatus.PENDING,
    "payment.pending": PaymentStatus.PENDING,
}

ENTITY_STATUS_MAP = {
    "captured": PaymentStatus.CAPTURED,
    "failed": PaymentStatus.FAILED,
    "authorized": PaymentStatus.PENDING,
    "pending": PaymentStatus.PENDING,
    "created": PaymentStatus.CREATED,
}

ENTITY_STATUS_BY_EVENT = {
    "payment.captured": "captured",
    "payment.failed": "failed",
    "payment.authorized": "authorized",
    "payment.pending": "pending",
}

TERMINAL_PAYMENT_STATUSES = {PaymentStatus.CAPTURED, PaymentStatus.FAILED}


class MalformedEventError(Exception):
    pass


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


FAILURE_REASON_MATCHERS = [
    ("insufficient", "INSUFFICIENT_FUNDS"),
    ("otp", "AUTHENTICATION_FAILED"),
    ("authentic", "AUTHENTICATION_FAILED"),
    ("mpin", "AUTHENTICATION_FAILED"),
    ("timeout", "BANK_TIMEOUT"),
    ("timed out", "BANK_TIMEOUT"),
    ("network", "NETWORK_ERROR"),
    ("connectivity", "NETWORK_ERROR"),
    ("declin", "CARD_DECLINED"),
    ("risk", "RISK_BLOCKED"),
    ("fraud", "RISK_BLOCKED"),
    ("blocked", "RISK_BLOCKED"),
    ("vpa", "INVALID_VPA"),
    ("limit", "LIMIT_EXCEEDED"),
]


def _failure_reason(error_code: str | None, error_description: str | None) -> str:
    text = " ".join(part for part in (error_code, error_description) if part).lower()
    for needle, reason in FAILURE_REASON_MATCHERS:
        if needle in text:
            return reason
    if error_code:
        cleaned = "".join(ch if ch.isalnum() else "_" for ch in error_code.upper()).strip("_")
        return ("RZP_" + cleaned)[:64]
    return "UNKNOWN"


def normalize(envelope: dict) -> dict:
    if not isinstance(envelope, dict):
        raise MalformedEventError("body must be a JSON object")
    event_type = envelope.get("event")
    if not isinstance(event_type, str) or not event_type:
        raise MalformedEventError("missing event")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise MalformedEventError("missing payload")
    payment_wrapper = payload.get("payment")
    if not isinstance(payment_wrapper, dict):
        raise MalformedEventError("missing payload.payment")
    entity = payment_wrapper.get("entity")
    if not isinstance(entity, dict):
        raise MalformedEventError("missing payload.payment.entity")
    payment_ref = entity.get("id")
    if not isinstance(payment_ref, str) or not payment_ref.strip():
        raise MalformedEventError("missing payment entity id")
    raw_status = entity.get("status")
    if not isinstance(raw_status, str) or not raw_status:
        raise MalformedEventError("missing payment entity status")
    amount = entity.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        raise MalformedEventError("invalid amount")
    created_at = entity.get("created_at")
    if created_at is None:
        created_at = int(datetime.now(timezone.utc).timestamp())
    if not isinstance(created_at, int) or isinstance(created_at, bool):
        raise MalformedEventError("invalid created_at")
    error_code = entity.get("error_code")
    error_code = error_code if isinstance(error_code, str) else None
    error_description = entity.get("error_description")
    error_description = error_description if isinstance(error_description, str) else None
    order_ref = entity.get("order_id")
    order_ref = order_ref if isinstance(order_ref, str) else None
    method = entity.get("method")
    method = method.upper() if isinstance(method, str) and method else None

    reject_reason = None
    derived_status = SUPPORTED_EVENTS.get(event_type)
    if derived_status is None:
        reject_reason = "UNSUPPORTED_EVENT_TYPE"
    else:
        entity_status = ENTITY_STATUS_MAP.get(raw_status)
        if entity_status is None:
            reject_reason = "UNMAPPABLE_STATUS"
        elif entity_status != derived_status:
            reject_reason = "EVENT_STATUS_MISMATCH"

    digest = hashlib.sha256(
        "|".join(
            [event_type, payment_ref, raw_status, str(amount), error_code or "", str(created_at)]
        ).encode("utf-8")
    ).hexdigest()

    return {
        "event_id": "evt_" + digest[:40],
        "event_type": event_type,
        "payment_ref": payment_ref,
        "raw_status": raw_status,
        "derived_status": derived_status,
        "reject_reason": reject_reason,
        "amount": (Decimal(amount) / Decimal(100)).quantize(Decimal("0.01")),
        "order_ref": order_ref,
        "method": method,
        "error_code": error_code,
        "error_description": error_description,
        "created_at": datetime.fromtimestamp(created_at, tz=timezone.utc),
    }


def _result(event: PaymentEvent, payment: Payment | None = None, processing_status: str | None = None, reason: str | None = None) -> dict:
    return {
        "event_id": event.event_id,
        "source": event.source,
        "event_type": event.event_type,
        "processing_status": processing_status or event.processing_status,
        "reason": reason if reason is not None else event.reason,
        "payment_id": event.payment_ref,
        "payment_status": payment.status.value if payment is not None else None,
    }


def _apply_event(db: Session, event: PaymentEvent, n: dict, mode: str = "autonomous") -> dict:
    now = datetime.now(timezone.utc)
    payment = db.query(Payment).filter(Payment.payment_id == n["payment_ref"]).one_or_none()

    if payment is None:
        order = None
        if n["order_ref"]:
            order = db.query(Order).filter(Order.order_id == n["order_ref"]).one_or_none()
            if order is None and mode == "autonomous":
                event.processing_status = "REJECTED"
                event.reason = "UNKNOWN_ORDER_REFERENCE"
                return _result(event)
        payment = Payment(
            payment_id=n["payment_ref"],
            order=order,
            amount=n["amount"],
            method=n["method"] or "UNKNOWN",
            status=n["derived_status"],
            failure_reason=(
                _failure_reason(n["error_code"], n["error_description"])
                if n["derived_status"] == PaymentStatus.FAILED
                else None
            ),
            gateway_metadata={
                "gateway": "razorpay",
                "mode": "test",
                "razorpay_order_id": n["order_ref"],
                "last_event_id": n["event_id"],
            },
            created_at=n["created_at"],
            updated_at=now,
        )
        db.add(payment)
        db.flush()
        record(
            db,
            "payment.created",
            {"payment_id": payment.payment_id, "status": payment.status.value, "source": event.source, "event_id": event.event_id},
            to_status=payment.status.value,
        )
    else:
        order = payment.order
        previous = payment.status
        if previous in TERMINAL_PAYMENT_STATUSES and n["derived_status"] != previous:
            event.processing_status = "REJECTED"
            event.reason = "TERMINAL_STATE_CONFLICT"
            return _result(event, payment)
        if n["derived_status"] != previous:
            payment.status = n["derived_status"]
            record(
                db,
                "payment.status_changed",
                {"payment_id": payment.payment_id, "source": event.source, "event_id": event.event_id},
                from_status=previous.value,
                to_status=n["derived_status"].value,
            )
        if n["derived_status"] == PaymentStatus.FAILED:
            if n["error_code"] or n["error_description"]:
                payment.failure_reason = _failure_reason(n["error_code"], n["error_description"])
            elif not payment.failure_reason:
                payment.failure_reason = "UNKNOWN"
        elif n["derived_status"] != previous:
            payment.failure_reason = None
        payment.updated_at = now
        metadata = dict(payment.gateway_metadata or {})
        metadata["last_event_id"] = n["event_id"]
        if n["order_ref"]:
            metadata["razorpay_order_id"] = n["order_ref"]
        payment.gateway_metadata = metadata

    if n["derived_status"] == PaymentStatus.CAPTURED and order is not None and order.status != OrderStatus.PAID:
        previous_order = order.status
        order.status = OrderStatus.PAID
        record(
            db,
            "order.status_changed",
            {"order_id": order.order_id, "trigger_event_id": event.event_id},
            from_status=previous_order.value,
            to_status=OrderStatus.PAID.value,
        )
    if n["derived_status"] == PaymentStatus.FAILED and order is not None and order.status == OrderStatus.CREATED:
        order.status = OrderStatus.ATTEMPTED
        record(
            db,
            "order.status_changed",
            {"order_id": order.order_id, "trigger_event_id": event.event_id},
            from_status=OrderStatus.CREATED.value,
            to_status=OrderStatus.ATTEMPTED.value,
        )

    event.payment_id = payment.id
    event.processing_status = "PROCESSED"
    event.processed_at = now
    db.flush()
    result = _result(event, payment)
    result["risk_evaluation"] = revenue_risk.evaluate(db, payment, event, mode)
    return result


def process_envelope(db: Session, envelope: dict, source: str, mode: str = "autonomous") -> dict:
    n = normalize(envelope)

    existing = db.query(PaymentEvent).filter(PaymentEvent.event_id == n["event_id"]).one_or_none()
    if existing is not None:
        payment = db.query(Payment).filter(Payment.payment_id == existing.payment_ref).one_or_none()
        result = _result(existing, payment, processing_status="DUPLICATE", reason=None)
        result["first_seen_at"] = existing.received_at.isoformat()
        return result

    event = PaymentEvent(
        event_id=n["event_id"],
        source=source,
        event_type=n["event_type"],
        payment_ref=n["payment_ref"],
        entity_status=n["raw_status"],
        raw_payload=envelope,
        processing_status="REJECTED",
        reason=n["reject_reason"],
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _result(event, processing_status="DUPLICATE", reason=None)

    if n["reject_reason"] is not None:
        db.commit()
        return _result(event)

    result = _apply_event(db, event, n, mode)
    db.commit()
    return result


def build_envelope(db: Session, spec: dict) -> dict:
    if spec.get("event") not in SUPPORTED_EVENTS:
        raise MalformedEventError("unsupported event type")
    payment = db.query(Payment).filter(Payment.payment_id == spec["payment_id"]).one_or_none()
    order = payment.order if payment is not None else None
    if order is None and spec.get("order_id"):
        order = db.query(Order).filter(Order.order_id == spec["order_id"]).one_or_none()

    if payment is None:
        if spec.get("amount_paise") is None or not spec.get("method"):
            raise MalformedEventError("unknown payment requires amount_paise and method")
        amount = spec["amount_paise"]
        method = spec["method"]
    else:
        amount = spec.get("amount_paise")
        if amount is None:
            amount = int((payment.amount * 100).to_integral_value())
        method = spec.get("method") or payment.method

    order_ref = spec.get("order_id") or (order.order_id if order is not None else None)
    entity = {
        "id": spec["payment_id"],
        "entity": "payment",
        "amount": amount,
        "currency": "INR",
        "status": ENTITY_STATUS_BY_EVENT[spec["event"]],
        "order_id": order_ref,
        "method": method.lower(),
        "created_at": spec.get("created_at") or int(datetime.now(timezone.utc).timestamp()),
    }
    if spec["event"] == "payment.failed":
        if spec.get("error_code"):
            entity["error_code"] = spec["error_code"]
        if spec.get("error_description"):
            entity["error_description"] = spec["error_description"]

    return {
        "entity": "event",
        "event": spec["event"],
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
    }
