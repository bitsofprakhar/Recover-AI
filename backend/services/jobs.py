"""Background job abstraction and execution (Phase 11, README Section "Phase 11").

A minimal, scheduler-agnostic job layer so Redis + Celery can replace the
in-process runner later without redesigning the workflow:

- jobs are **durable rows** in `background_jobs` (created transactionally with
  the pipeline step that schedules them, so a crash never loses work);
- every job has a deterministic **key** - scheduling the same key twice is a
  no-op (idempotency), exactly like the gate/executor keys of Phases 7-8;
- execution goes through a **registry** of named handlers that call the
  existing services (agent run, verification, outcome simulator, expiry
  sweep); each job runs in its own transaction and a failure is recorded on
  the row without affecting other jobs;
- **recurring** jobs reschedule themselves by interval (the expiry sweep).

The default runner is the in-process asyncio scheduler started with the FastAPI
app (`services/scheduler.py`); `run_due_jobs` is also callable synchronously
via `POST /api/jobs/run` so tests and the demo stay deterministic.
"""
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session

from config import settings
from models import BackgroundJob, RecoveryCase, RecoveryCaseStatus, TERMINAL_CASE_STATUSES

JOB_STATUS_PENDING = "PENDING"
JOB_STATUS_DONE = "DONE"
JOB_STATUS_FAILED = "FAILED"

SWEEP_JOB_NAME = "expiry_sweep"
SWEEP_JOB_KEY = "sweep:expiry"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def agent_job_key(case_id: int, attempt: int) -> str:
    return f"agent:{case_id}:{attempt}"


def verify_job_key(case_id: int, attempt: int, origin: str = "executed") -> str:
    return f"verify:{case_id}:{attempt}:{origin}"


def outcome_job_key(case_id: int, attempt: int, outcome: str) -> str:
    return f"outcome:{case_id}:{attempt}:{outcome}"


def _job_info(job: BackgroundJob) -> dict:
    return {
        "id": job.id,
        "job_key": job.job_key,
        "name": job.name,
        "params": job.params,
        "status": job.status,
        "due_at": job.due_at.isoformat(),
        "recurring_interval_seconds": job.recurring_interval_seconds,
        "result": job.result,
        "error": job.error,
    }


def schedule_job(
    db: Session,
    name: str,
    params: dict | None,
    due_at: datetime,
    key: str,
    recurring_interval_seconds: int | None = None,
) -> dict:
    """Schedule a job (idempotent by key). Returns job info plus `created`."""
    existing = db.query(BackgroundJob).filter(BackgroundJob.job_key == key).one_or_none()
    if existing is not None:
        return {**_job_info(existing), "created": False}
    job = BackgroundJob(
        job_key=key,
        name=name,
        params=params,
        status=JOB_STATUS_PENDING,
        due_at=due_at,
        recurring_interval_seconds=recurring_interval_seconds,
    )
    db.add(job)
    db.flush()
    return {**_job_info(job), "created": True}


def list_jobs(db: Session, status: str | None = None, limit: int = 100) -> dict:
    query = db.query(BackgroundJob).order_by(BackgroundJob.due_at, BackgroundJob.id)
    if status is not None:
        query = query.filter(BackgroundJob.status == status)
    total = query.count()
    jobs = query.limit(limit).all()
    return {"total": total, "items": [_job_info(job) for job in jobs]}


def run_due_jobs(db: Session, now: datetime | None = None, force: bool = False) -> dict:
    """Execute every due (or, with force, every) PENDING job.

    Each job runs in its own transaction: a handler failure rolls that job
    back, is recorded as FAILED on the row, and never blocks the other jobs.
    Recurring jobs flip back to PENDING with the next due time.
    """
    now = now or _now()
    query = db.query(BackgroundJob).filter(BackgroundJob.status == JOB_STATUS_PENDING).order_by(
        BackgroundJob.due_at, BackgroundJob.id
    )
    jobs = query.all() if force else query.filter(BackgroundJob.due_at <= now).all()
    executed = []
    for job in jobs:
        handler = JOB_HANDLERS.get(job.name)
        if handler is None:
            job.status = JOB_STATUS_FAILED
            job.error = f"UNKNOWN_JOB: {job.name}"
            db.commit()
            executed.append(_job_info(job))
            continue
        job_id = job.id
        try:
            result = handler(db, job.params or {})
            if job.recurring_interval_seconds is not None:
                job.status = JOB_STATUS_PENDING
                job.due_at = now + timedelta(seconds=job.recurring_interval_seconds)
            else:
                job.status = JOB_STATUS_DONE
            job.result = result if isinstance(result, dict) else {"value": result}
            job.error = None
            db.commit()
        except Exception as exc:  # noqa: BLE001 - recorded on the job row
            db.rollback()
            job = db.query(BackgroundJob).filter(BackgroundJob.id == job_id).one()
            job.status = JOB_STATUS_FAILED
            job.error = str(exc)[:512]
            if job.recurring_interval_seconds is not None:
                job.status = JOB_STATUS_PENDING
                job.due_at = now + timedelta(seconds=job.recurring_interval_seconds)
            db.commit()
        executed.append(_job_info(job))
    pending = db.query(BackgroundJob).filter(BackgroundJob.status == JOB_STATUS_PENDING).count()
    return {"executed": executed, "executed_count": len(executed), "pending": pending, "forced": force}


def ensure_recurring_jobs(db: Session) -> dict:
    """Seed the recurring expiry sweep job if it does not exist yet."""
    return schedule_job(
        db,
        SWEEP_JOB_NAME,
        {},
        _now(),
        SWEEP_JOB_KEY,
        recurring_interval_seconds=settings.sweep_interval_seconds,
    )


def sweep_expired_cases(db: Session, now: datetime | None = None) -> dict:
    """General expiry sweep (README rule 16): stop every active case whose
    24-hour window has expired, through the audited state machine.

    Rule 16 applies from any active state; terminal cases are never touched.
    """
    from services.case_lifecycle import IllegalTransitionError, transition

    now = now or _now()
    expired = (
        db.query(RecoveryCase)
        .filter(RecoveryCase.status.notin_(list(TERMINAL_CASE_STATUSES)), RecoveryCase.expiry <= now)
        .order_by(RecoveryCase.id)
        .all()
    )
    stopped: list[dict] = []
    skipped: list[dict] = []
    for case in expired:
        previous = case.status
        try:
            transition(
                db,
                case,
                RecoveryCaseStatus.STOPPED,
                "case.window_expired",
                payload={"reason": "CASE_WINDOW_EXPIRED", "detected_by": "expiry_sweep"},
            )
            stopped.append({"case_id": case.id, "from_status": previous.value})
        except IllegalTransitionError as exc:
            skipped.append({"case_id": case.id, "reason": str(exc)})
    db.commit()
    return {
        "checked_at": now.isoformat(),
        "expired_cases": len(expired),
        "stopped": stopped,
        "skipped": skipped,
        "note": "general expiry sweep (rule 16): active cases past the 24-hour window are stopped with an audited transition",
    }


def _handle_agent_run(db: Session, params: dict) -> dict:
    from services.agent import run_agent

    result = run_agent(db, params["case_id"])
    db.commit()
    return {
        "decision": result.get("decision"),
        "case_id": params["case_id"],
        "reasoning_source": (result.get("diagnosis") or {}).get("reasoning_source"),
        "note": "background agent run: diagnosis -> score -> action selection -> safety gate",
    }


def _handle_verify_outcome(db: Session, params: dict) -> dict:
    from services.verification import verify_outcome

    case = db.query(RecoveryCase).filter(RecoveryCase.id == params["case_id"]).one_or_none()
    if case is None:
        return {"decision": "NOOP", "reason": f"case {params['case_id']} not found"}
    result = verify_outcome(db, case)
    db.commit()
    return {
        "decision": result.get("decision"),
        "result": (result.get("verification") or {}).get("result"),
        "case_id": params["case_id"],
    }


def _handle_simulate_outcome(db: Session, params: dict) -> dict:
    from services.outcome_simulator import simulate_outcome

    case = db.query(RecoveryCase).filter(RecoveryCase.id == params["case_id"]).one_or_none()
    if case is None:
        return {"decision": "NOOP", "reason": f"case {params['case_id']} not found"}
    result = simulate_outcome(db, case, params["outcome"], created_at=params.get("created_at"))
    db.commit()
    return {
        "decision": result.get("decision"),
        "outcome": params["outcome"],
        "case_id": params["case_id"],
        "simulated": True,
    }


def _handle_expiry_sweep(db: Session, params: dict) -> dict:
    return sweep_expired_cases(db)


JOB_HANDLERS: dict[str, Callable[[Session, dict], dict]] = {
    "run_agent": _handle_agent_run,
    "verify_outcome": _handle_verify_outcome,
    "simulate_outcome": _handle_simulate_outcome,
    SWEEP_JOB_NAME: _handle_expiry_sweep,
}
