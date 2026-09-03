"""Outcome simulator (Phase 8, README rules 13-16 and Section 11 preview).

Injects the scripted outcome of an executed recovery action - SUCCESS,
FAILED, STILL_PENDING or NO_RESPONSE - through the real event pipeline
(webhook replay), so payment/order state changes carry the full Phase 3
idempotency and audit machinery. Everything the simulator produces is
explicitly labelled simulated; no real payment operation occurs. A SUCCESS
outcome captures the simulated recovery payment (or simulates an independent
customer retry when no recovery payment exists) but never marks revenue
recovered - verification and attribution (rules 12, 18-20) live in the Phase 9
verification service. A FAILED outcome applies the retry cycle: attempts
remaining and window open -> DIAGNOSING for a new attempt, attempts exhausted
-> NOT_RECOVERED.

Phase 11 addition: a SUCCESS outcome schedules an immediate verification job
and a retry-eligible FAILED outcome schedules the next agent run, so the loop
continues asynchronously.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from config import settings
from models import TERMINAL_CASE_STATUSES, AgentAction, Payment, RecoveryCase, RecoveryCaseStatus
from services.audit import record
from services.case_lifecycle import transition
from services.event_intake import build_envelope, process_envelope

VALID_OUTCOMES = ("SUCCESS", "FAILED", "STILL_PENDING", "NO_RESPONSE")
SIMULATOR_TOOL_NAME = "simulate_outcome"


def _source_payment(db: Session, case: RecoveryCase) -> Payment:
    return db.query(Payment).filter(Payment.id == case.payment_id).one()


def _recovery_payments(db: Session, case: RecoveryCase) -> list[Payment]:
    payment = _source_payment(db, case)
    if payment.order_id is None:
        return []
    rows = db.query(Payment).filter(Payment.order_id == payment.order_id).order_by(Payment.id).all()
    return [row for row in rows if (row.gateway_metadata or {}).get("recovery_case_id") == case.id]


def _replay_event(db: Session, spec: dict) -> dict:
    envelope = build_envelope(db, spec)
    return process_envelope(db, envelope, "REPLAY")


def simulate_outcome(db: Session, case: RecoveryCase, outcome: str, created_at: int | None = None) -> dict:
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"unknown outcome {outcome}; expected one of {VALID_OUTCOMES}")

    if case.status in TERMINAL_CASE_STATUSES:
        return {
            "case_id": case.id,
            "decision": "NOOP",
            "case_status": case.status.value,
            "reason": f"case is terminal ({case.status.value}); no outcome can be simulated",
            "outcome": None,
        }
    if case.status != RecoveryCaseStatus.WAITING_FOR_RESULT:
        return {
            "case_id": case.id,
            "decision": "NOOP",
            "case_status": case.status.value,
            "reason": f"outcomes are simulated only for cases waiting for a result (current: {case.status.value})",
            "outcome": None,
        }

    now = datetime.now(timezone.utc)
    if case.expiry <= now:
        transition(
            db,
            case,
            RecoveryCaseStatus.STOPPED,
            "case.window_expired",
            payload={"outcome": outcome, "reason": "CASE_WINDOW_EXPIRED"},
        )
        db.commit()
        return {
            "case_id": case.id,
            "decision": "STOPPED",
            "case_status": case.status.value,
            "reason": "the 24-hour case window expired before the outcome arrived",
            "outcome": {"outcome": outcome, "simulated": True},
        }

    result = {
        "tool": SIMULATOR_TOOL_NAME,
        "outcome": outcome,
        "simulated": True,
        "event_processing": None,
        "case_transition": None,
    }

    if outcome == "SUCCESS":
        recovery = _recovery_payments(db, case)
        pending = [row for row in recovery if row.status.value == "PENDING"]
        if recovery and not pending:
            return {
                "case_id": case.id,
                "decision": "NOOP",
                "case_status": case.status.value,
                "reason": "the recovery payment already reached a terminal state; nothing to simulate",
                "outcome": None,
            }
        if recovery:
            spec = {"payment_id": pending[-1].payment_id, "event": "payment.captured"}
        else:
            source = _source_payment(db, case)
            order = source.order
            spec = {
                "payment_id": f"pay_retry_{case.id}_{case.attempt_count}",
                "event": "payment.captured",
                "order_id": order.order_id if order is not None else None,
                "amount_paise": int((case.revenue_at_risk * 100).to_integral_value()),
                "method": source.method,
            }
        if created_at is not None:
            spec["created_at"] = created_at
        processed = _replay_event(db, spec)
        result["event_processing"] = {
            "payment_id": spec["payment_id"],
            "processing_status": processed.get("processing_status"),
            "payment_status": processed.get("payment_status"),
        }
        result["case_transition"] = None
        record(
            db,
            "outcome.success",
            case_id=case.id,
            from_status=RecoveryCaseStatus.WAITING_FOR_RESULT,
            payload={
                "outcome": outcome,
                "simulated": True,
                "payment_id": spec["payment_id"],
                "note": "simulated success injected via event replay; the case stays WAITING_FOR_RESULT until verification and attribution runs (POST /api/cases/{id}/verify)",
            },
        )
        from services.jobs import schedule_job, verify_job_key

        schedule_job(
            db,
            "verify_outcome",
            {"case_id": case.id},
            datetime.now(timezone.utc),
            verify_job_key(case.id, case.attempt_count, "outcome"),
        )
    elif outcome == "FAILED":
        recovery = _recovery_payments(db, case)
        pending = [row for row in recovery if row.status.value == "PENDING"]
        if pending:
            spec = {
                "payment_id": pending[-1].payment_id,
                "event": "payment.failed",
                "error_code": "SIMULATED_RECOVERY_FAILURE",
                "error_description": "Simulated recovery payment failure",
            }
            if created_at is not None:
                spec["created_at"] = created_at
            processed = _replay_event(db, spec)
            result["event_processing"] = {
                "payment_id": spec["payment_id"],
                "processing_status": processed.get("processing_status"),
                "payment_status": processed.get("payment_status"),
            }
        if case.attempt_count >= settings.max_recovery_attempts:
            transition(
                db,
                case,
                RecoveryCaseStatus.NOT_RECOVERED,
                "outcome.not_recovered",
                payload={"outcome": outcome, "simulated": True, "attempt_count": case.attempt_count},
            )
            result["case_transition"] = "NOT_RECOVERED"
        else:
            transition(
                db,
                case,
                RecoveryCaseStatus.DIAGNOSING,
                "outcome.retry",
                payload={"outcome": outcome, "simulated": True, "attempt_count": case.attempt_count},
            )
            result["case_transition"] = "DIAGNOSING"
            from services.jobs import agent_job_key, schedule_job

            schedule_job(
                db,
                "run_agent",
                {"case_id": case.id},
                datetime.now(timezone.utc),
                agent_job_key(case.id, case.attempt_count),
            )
    else:
        event = "outcome.still_pending" if outcome == "STILL_PENDING" else "outcome.no_response"
        result["case_transition"] = None
        record(
            db,
            event,
            case_id=case.id,
            from_status=RecoveryCaseStatus.WAITING_FOR_RESULT,
            payload={"outcome": outcome, "simulated": True, "note": "monitoring continues (rule 15)"},
        )

    db.add(
        AgentAction(
            case_id=case.id,
            tool_name=SIMULATOR_TOOL_NAME,
            input={"outcome": outcome, "simulated": True},
            output=result,
            allowed=None,
        )
    )
    db.flush()
    db.commit()
    return {
        "case_id": case.id,
        "decision": result["case_transition"] or "WAITING_FOR_RESULT",
        "case_status": case.status.value,
        "outcome": result,
    }
