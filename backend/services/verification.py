"""Outcome verification & attribution (Phase 9, README rules 12 and 18-20, Section 11).

Verification is the explicit step after an executed recovery action produced an
outcome: it reads the latest payment/order state and applies the attribution
rule deterministically. A successful payment qualifies as recovered only when it
is attributable to the recovery case:

  1. associated    - linked to the same order as the case's source payment
                     (executor-created recovery payments additionally carry
                     gateway_metadata.recovery_case_id);
  2. after action  - captured after the case's earliest approved (gate-ALLOWed,
                     executor-executed) recovery action;
  3. within window - captured within the configured case window;
  4. never twice   - not already credited to another case (a schema-level
                     unique index on recovery_cases.recovered_payment_id
                     enforces this, and a RECOVERED case is terminal).

A verified success that fails attribution is NOT credited (rule 18,
NOT_RECOVERED); verification of an expired waiting case stops it (rule 20,
rule 16 applied at verification time); a waiting case without any successful
payment keeps monitoring (rule 15). Ambiguous or exceptional conditions
escalate (rule 17). Every verification run on a waiting case is logged to
agent_actions, and every transition through the audited state machine.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from models import (
    TERMINAL_CASE_STATUSES,
    AgentAction,
    AuditLog,
    OrderStatus,
    Payment,
    PaymentEvent,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.agent.tools import jsonable
from services.case_lifecycle import transition

VERIFICATION_TOOL_NAME = "verify_outcome"
SIMULATED_EVENT_SOURCES = ("SYNTHETIC", "REPLAY")


def _capture_time(db: Session, payment: Payment) -> tuple[datetime, str, str | None]:
    """When the system observed the capture, and on what basis.

    Primary source: the latest PROCESSED payment.captured event for the payment
    (its received_at, microsecond precision, set by the event pipeline). A
    payment with no capture event (seeded or directly inserted rows) falls back
    to the payment row's updated_at.
    """
    event = (
        db.query(PaymentEvent)
        .filter(
            PaymentEvent.payment_ref == payment.payment_id,
            PaymentEvent.event_type == "payment.captured",
            PaymentEvent.processing_status == "PROCESSED",
        )
        .order_by(PaymentEvent.received_at.desc(), PaymentEvent.id.desc())
        .first()
    )
    if event is not None:
        return event.received_at, "payment.captured event received_at", event.source
    return payment.updated_at, "payment row updated_at (no capture event stored)", None


def _action_executed_at(db: Session, case: RecoveryCase) -> datetime | None:
    """Time of the case's earliest approved (executor-executed) recovery action."""
    row = (
        db.query(AuditLog)
        .filter(AuditLog.case_id == case.id, AuditLog.event_type == "action.executed")
        .order_by(AuditLog.created_at, AuditLog.id)
        .first()
    )
    return row.created_at if row is not None else None


def _rejection_reason(checks: dict) -> str:
    if not checks["after_approved_action"]:
        return "CAPTURED_BEFORE_APPROVED_ACTION"
    if not checks["within_case_window"]:
        return "OUTSIDE_CASE_WINDOW"
    if not checks["not_already_credited"]:
        return "ALREADY_CREDITED_TO_ANOTHER_CASE"
    if not checks["associated"]:
        return "NOT_ASSOCIATED_WITH_CASE"
    return "NOT_ATTRIBUTABLE"


def _evaluate_candidate(db: Session, case: RecoveryCase, source: Payment, payment: Payment, action_at: datetime) -> dict:
    capture_time, basis, event_source = _capture_time(db, payment)
    metadata = payment.gateway_metadata or {}
    credited = (
        db.query(RecoveryCase)
        .filter(RecoveryCase.recovered_payment_id == payment.payment_id, RecoveryCase.id != case.id)
        .count()
        > 0
    )
    checks = {
        "associated": payment.order_id == source.order_id or metadata.get("recovery_case_id") == case.id,
        "after_approved_action": capture_time > action_at,
        "within_case_window": capture_time <= case.expiry,
        "not_already_credited": not credited,
    }
    return {
        "payment_id": payment.payment_id,
        "amount": str(payment.amount),
        "status": payment.status.value,
        "capture_time": capture_time,
        "capture_time_basis": basis,
        "event_source": event_source,
        "simulated": event_source in SIMULATED_EVENT_SOURCES if event_source is not None else None,
        "recovery_case_payment": metadata.get("recovery_case_id") == case.id,
        "checks": checks,
        "attributable": all(checks.values()),
        "rejection_reason": None if all(checks.values()) else _rejection_reason(checks),
    }


def _recovery_summary(case: RecoveryCase) -> dict:
    return {
        "recovered": True,
        "payment_id": case.recovered_payment_id,
        "amount": str(case.recovered_amount) if case.recovered_amount is not None else None,
        "recovered_at": case.recovered_at.isoformat() if case.recovered_at is not None else None,
    }


def _log_verification(db: Session, case: RecoveryCase, status_at_entry: RecoveryCaseStatus, output: dict) -> None:
    db.add(
        AgentAction(
            case_id=case.id,
            tool_name=VERIFICATION_TOOL_NAME,
            input={"case_id": case.id, "case_status": status_at_entry.value},
            output=jsonable(output),
            allowed=None,
        )
    )
    db.flush()


def verify_outcome(db: Session, case: RecoveryCase) -> dict:
    """Verify the outcome of an executed recovery action and attribute revenue.

    Runs only on cases in WAITING_FOR_RESULT; terminal and non-waiting cases
    are structured NOOPs. Idempotent by construction: a RECOVERED case is
    terminal and never re-verifies, and a waiting case without verifiable
    evidence simply keeps monitoring.
    """
    status_at_entry = case.status

    if case.status in TERMINAL_CASE_STATUSES:
        result = {
            "case_id": case.id,
            "decision": "NOOP",
            "case_status": case.status.value,
            "reason": f"case is terminal ({case.status.value}); the outcome is already verified",
            "verification": _recovery_summary(case) if case.status == RecoveryCaseStatus.RECOVERED else None,
        }
        return result
    if case.status != RecoveryCaseStatus.WAITING_FOR_RESULT:
        return {
            "case_id": case.id,
            "decision": "NOOP",
            "case_status": case.status.value,
            "reason": f"verification runs only on cases waiting for a result (current: {case.status.value})",
            "verification": None,
        }

    now = datetime.now(timezone.utc)
    if case.expiry <= now:
        transition(
            db,
            case,
            RecoveryCaseStatus.STOPPED,
            "case.window_expired",
            payload={"reason": "CASE_WINDOW_EXPIRED", "detected_by": "verification"},
        )
        result = {
            "case_id": case.id,
            "decision": "STOPPED",
            "case_status": case.status.value,
            "reason": "the 24-hour case window expired before verification could attribute an outcome",
            "verification": {"result": "CASE_WINDOW_EXPIRED"},
        }
        _log_verification(db, case, status_at_entry, result)
        db.commit()
        return result

    source = db.query(Payment).filter(Payment.id == case.payment_id).one()
    if source.order_id is None:
        transition(
            db,
            case,
            RecoveryCaseStatus.ESCALATED,
            "verification.escalated",
            payload={"reason": "MISSING_ORDER_IDENTITY", "rule": 17},
        )
        result = {
            "case_id": case.id,
            "decision": "ESCALATED",
            "case_status": case.status.value,
            "reason": "the source payment lost its order identity; attribution is impossible (ambiguity trigger)",
            "verification": {"result": "MISSING_ORDER_IDENTITY"},
        }
        _log_verification(db, case, status_at_entry, result)
        db.commit()
        return result

    action_at = _action_executed_at(db, case)
    if action_at is None:
        transition(
            db,
            case,
            RecoveryCaseStatus.ESCALATED,
            "verification.escalated",
            payload={"reason": "NO_APPROVED_ACTION_RECORDED", "rule": 17},
        )
        result = {
            "case_id": case.id,
            "decision": "ESCALATED",
            "case_status": case.status.value,
            "reason": "no approved recovery action is recorded for this waiting case; attribution is impossible",
            "verification": {"result": "NO_APPROVED_ACTION_RECORDED"},
        }
        _log_verification(db, case, status_at_entry, result)
        db.commit()
        return result

    order = source.order
    rows = db.query(Payment).filter(Payment.order_id == source.order_id).order_by(Payment.id).all()
    captured = [row for row in rows if row.id != source.id and row.status == PaymentStatus.CAPTURED]

    if not captured:
        if order is not None and order.status == OrderStatus.PAID:
            transition(
                db,
                case,
                RecoveryCaseStatus.ESCALATED,
                "verification.escalated",
                payload={"reason": "CONFLICTING_ORDER_STATE", "detail": "order PAID but no captured payment found", "rule": 17},
            )
            result = {
                "case_id": case.id,
                "decision": "ESCALATED",
                "case_status": case.status.value,
                "reason": "the order is PAID but no captured payment is associated with it; conflicting state (ambiguity trigger)",
                "verification": {"result": "CONFLICTING_ORDER_STATE"},
            }
            _log_verification(db, case, status_at_entry, result)
            db.commit()
            return result
        result = {
            "case_id": case.id,
            "decision": "WAITING_FOR_RESULT",
            "case_status": case.status.value,
            "reason": "no successful payment on the order yet; monitoring continues (rule 15)",
            "verification": {
                "result": "NO_SUCCESS_YET",
                "order_id": order.order_id if order is not None else None,
                "action_executed_at": action_at,
                "case_expires_at": case.expiry,
                "note": "re-verify after the next outcome or webhook; nothing is credited without a verified attributable success",
            },
        }
        _log_verification(db, case, status_at_entry, result)
        db.commit()
        return result

    evaluated = [_evaluate_candidate(db, case, source, payment, action_at) for payment in captured]
    attributable = [item for item in evaluated if item["attributable"]]

    if attributable:
        evidence = min(attributable, key=lambda item: item["capture_time"])
        case.recovered_payment_id = evidence["payment_id"]
        case.recovered_amount = Decimal(evidence["amount"])
        case.recovered_at = evidence["capture_time"]
        transition(
            db,
            case,
            RecoveryCaseStatus.RECOVERED,
            "verification.recovered",
            payload=jsonable(
                {
                    "rule": 12,
                    "payment_id": evidence["payment_id"],
                    "amount": evidence["amount"],
                    "capture_time": evidence["capture_time"],
                    "capture_time_basis": evidence["capture_time_basis"],
                    "event_source": evidence["event_source"],
                    "simulated": evidence["simulated"],
                    "action_executed_at": action_at,
                    "case_expires_at": case.expiry,
                    "attribution_checks": evidence["checks"],
                    "other_candidates": [item["payment_id"] for item in attributable if item is not evidence],
                }
            ),
        )
        result = {
            "case_id": case.id,
            "decision": "RECOVERED",
            "case_status": case.status.value,
            "reason": "verified successful payment attributable to an approved recovery action within the case window",
            "verification": {
                "result": "RECOVERED",
                "attribution": evidence,
                "recovery": _recovery_summary(case),
                "note": "revenue is credited only now, after verification and attribution - never on execution or capture alone",
            },
        }
    else:
        reasons = {item["payment_id"]: item["rejection_reason"] for item in evaluated}
        transition(
            db,
            case,
            RecoveryCaseStatus.NOT_RECOVERED,
            "verification.not_recovered",
            payload=jsonable(
                {
                    "rule": 18,
                    "reason": "SUCCESS_NOT_ATTRIBUTABLE",
                    "candidate_rejection_reasons": reasons,
                    "action_executed_at": action_at,
                    "case_expires_at": case.expiry,
                }
            ),
        )
        result = {
            "case_id": case.id,
            "decision": "NOT_RECOVERED",
            "case_status": case.status.value,
            "reason": "a successful payment exists on the order but is not attributable to an approved recovery action; the agent is not credited",
            "verification": {
                "result": "SUCCESS_NOT_ATTRIBUTABLE",
                "candidates": evaluated,
                "note": "independent customer activity outside the attributable action window is never credited",
            },
        }

    _log_verification(db, case, status_at_entry, result)
    db.commit()
    return result
