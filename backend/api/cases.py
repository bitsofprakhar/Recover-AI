"""Recovery case APIs: list, detail with the agent/audit timeline, agent run."""
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from models import RecoveryCase, RecoveryCaseStatus
from services.action_executor import execute_action
from services.agent import CaseNotFoundError, run_agent
from services.case_lifecycle import IllegalTransitionError
from services.outcome_simulator import simulate_outcome
from services.safety_gate import submit_to_gate
from services.verification import verify_outcome

router = APIRouter(prefix="/api/cases", tags=["cases"])


def _parse_diagnosis(case: RecoveryCase):
    if not case.diagnosis:
        return None
    try:
        return json.loads(case.diagnosis)
    except ValueError:
        return case.diagnosis


def _serialize(db: Session, case: RecoveryCase) -> dict:
    payment = case.payment
    order = payment.order if payment is not None else None
    customer = order.customer if order is not None else None
    return {
        "id": case.id,
        "status": case.status.value,
        "revenue_at_risk": str(case.revenue_at_risk),
        "diagnosis": _parse_diagnosis(case),
        "score": case.score,
        "selected_action": case.selected_action,
        "attempt_count": case.attempt_count,
        "expiry": case.expiry.isoformat(),
        "recovered_payment_id": case.recovered_payment_id,
        "recovered_amount": str(case.recovered_amount) if case.recovered_amount is not None else None,
        "recovered_at": case.recovered_at.isoformat() if case.recovered_at is not None else None,
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
        "payment": (
            {
                "payment_id": payment.payment_id,
                "status": payment.status.value,
                "amount": str(payment.amount),
                "method": payment.method,
                "failure_reason": payment.failure_reason,
            }
            if payment is not None
            else None
        ),
        "order": (
            {
                "order_id": order.order_id,
                "status": order.status.value,
                "amount": str(order.amount),
                "currency": order.currency,
            }
            if order is not None
            else None
        ),
        "customer": (
            {
                "customer_id": customer.customer_id,
                "name": customer.name,
                "email": customer.email,
                "phone": customer.phone,
            }
            if customer is not None
            else None
        ),
    }


@router.get("")
def list_cases(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(RecoveryCase).order_by(RecoveryCase.id.desc())
    if status is not None:
        valid = [item.value for item in RecoveryCaseStatus]
        if status not in valid:
            raise HTTPException(status_code=422, detail=f"unknown status {status}; expected one of {valid}")
        query = query.filter(RecoveryCase.status == RecoveryCaseStatus(status))
    total = query.count()
    cases = query.offset(offset).limit(limit).all()
    return {"total": total, "items": [_serialize(db, case) for case in cases]}


@router.get("/{case_id}")
def get_case(case_id: int, db: Session = Depends(get_db)) -> dict:
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).one_or_none()
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    data = _serialize(db, case)
    data["agent_actions"] = [
        {
            "id": action.id,
            "tool_name": action.tool_name,
            "input": action.input,
            "output": action.output,
            "allowed": action.allowed,
            "created_at": action.created_at.isoformat(),
        }
        for action in sorted(case.agent_actions, key=lambda item: item.id)
    ]
    data["audit_logs"] = [
        {
            "id": log.id,
            "event_type": log.event_type,
            "from_status": log.from_status,
            "to_status": log.to_status,
            "payload": log.payload,
            "created_at": log.created_at.isoformat(),
        }
        for log in sorted(case.audit_logs, key=lambda item: item.id)
    ]
    return data


class OutcomeRequest(BaseModel):
    outcome: Literal["SUCCESS", "FAILED", "STILL_PENDING", "NO_RESPONSE"]
    created_at: int | None = None
    delay_seconds: int | None = None


@router.post("/{case_id}/action/execute")
def execute_action_endpoint(case_id: int, db: Session = Depends(get_db)) -> dict:
    """Execute the gate-ALLOWed recovery action (simulated; idempotent)."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).one_or_none()
    if case is None:
        raise HTTPException(status_code=404, detail=f"case {case_id} not found")
    try:
        return execute_action(db, case)
    except IllegalTransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{case_id}/outcome")
def simulate_outcome_endpoint(case_id: int, body: OutcomeRequest, db: Session = Depends(get_db)) -> dict:
    """Inject the scripted outcome of an executed recovery action (simulated).

    With `delay_seconds` > 0 the outcome is scheduled as a background job that
    the scheduler executes when due (Phase 11) instead of running now.
    """
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).one_or_none()
    if case is None:
        raise HTTPException(status_code=404, detail=f"case {case_id} not found")
    if body.delay_seconds is not None and body.delay_seconds > 0:
        from datetime import datetime, timedelta, timezone

        from services.jobs import outcome_job_key, schedule_job

        job = schedule_job(
            db,
            "simulate_outcome",
            {"case_id": case.id, "outcome": body.outcome, "created_at": body.created_at},
            datetime.now(timezone.utc) + timedelta(seconds=body.delay_seconds),
            outcome_job_key(case.id, case.attempt_count, body.outcome),
        )
        db.commit()
        return {
            "case_id": case.id,
            "decision": "SCHEDULED",
            "case_status": case.status.value,
            "scheduled_outcome": job,
            "note": "simulated outcome scheduled; the background scheduler executes it when due (POST /api/jobs/run with force to run it now)",
        }
    try:
        return simulate_outcome(db, case, body.outcome, created_at=body.created_at)
    except (IllegalTransitionError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{case_id}/verify")
def verify_outcome_endpoint(case_id: int, db: Session = Depends(get_db)) -> dict:
    """Verify the outcome of an executed recovery action and attribute revenue (Phase 9)."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).one_or_none()
    if case is None:
        raise HTTPException(status_code=404, detail=f"case {case_id} not found")
    try:
        return verify_outcome(db, case)
    except IllegalTransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{case_id}/agent/run")
def run_agent_endpoint(case_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return run_agent(db, case_id)
    except CaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except IllegalTransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{case_id}/gate/evaluate")
def gate_evaluate_endpoint(case_id: int, db: Session = Depends(get_db)) -> dict:
    """Submit the case's selected action to the safety gate (idempotent)."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).one_or_none()
    if case is None:
        raise HTTPException(status_code=404, detail=f"case {case_id} not found")
    try:
        result = submit_to_gate(db, case)
    except IllegalTransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    db.commit()
    return result
