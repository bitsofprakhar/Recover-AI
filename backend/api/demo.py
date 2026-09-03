"""Deterministic demo-case API (post-Phase-12 reliability fix)."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db import get_db
from models import RecoveryCase, RecoveryCaseStatus
from services.demo import demo_eligible_payments
from services.event_intake import build_envelope, process_envelope
from services.jobs import run_due_jobs

router = APIRouter(prefix="/api/demo", tags=["demo"])

MAX_CANDIDATES_PER_CALL = 15


@router.post("/case")
def create_demo_case(db: Session = Depends(get_db)) -> dict:
    """Create the next deterministic executable demo case through the real pipeline.

    Picks the next failed payment that - given the current database state - is
    guaranteed to produce a fresh, clean recovery case, posts its failure event
    through the real event intake (risk evaluation included), runs the
    scheduled agent job through the Phase 11 job layer, and returns the first
    case that reaches SAFETY_CHECK, the state from which
    POST /api/cases/{id}/action/execute works.

    Nothing is weakened: the event, the risk evaluation, the agent, the
    scoring and the safety gate all run their normal rules; candidates the
    agent escalates or stops are recorded in `attempted` and skipped.
    Repeatable without a database reset: each call consumes a different
    payment from the deterministic pool.
    """
    attempted: list[dict] = []
    for payment in demo_eligible_payments(db)[:MAX_CANDIDATES_PER_CALL]:
        envelope = build_envelope(
            db,
            {
                "payment_id": payment.payment_id,
                "event": "payment.failed",
                "error_description": payment.failure_reason or "Insufficient funds",
            },
        )
        result = process_envelope(db, envelope, "SYNTHETIC")
        risk = result.get("risk_evaluation") or {}
        if risk.get("decision") != "CASE_CREATED" or risk.get("case_id") is None:
            attempted.append({"payment_id": payment.payment_id, "risk_decision": risk.get("decision")})
            continue

        run_due_jobs(db)
        db.expire_all()
        case = db.query(RecoveryCase).filter(RecoveryCase.id == risk["case_id"]).one()
        if case.status == RecoveryCaseStatus.SAFETY_CHECK:
            return {
                "created": True,
                "case_id": case.id,
                "payment_id": payment.payment_id,
                "case_status": case.status.value,
                "selected_action": case.selected_action,
                "score": case.score,
                "attempted": attempted,
                "note": (
                    "deterministic demo case in SAFETY_CHECK: drive it with POST /api/cases/{id}/action/execute, "
                    "then POST /api/cases/{id}/outcome and POST /api/cases/{id}/verify (or let the scheduler)"
                ),
            }
        attempted.append(
            {"payment_id": payment.payment_id, "case_id": case.id, "case_status": case.status.value}
        )

    return {
        "created": False,
        "reason": "NO_EXECUTABLE_DEMO_CASE",
        "attempted": attempted,
        "hint": (
            "no failed payment can produce a fresh executable case in the current database state; "
            "reset the demo data with: python -m database.seed --reset"
        ),
    }
