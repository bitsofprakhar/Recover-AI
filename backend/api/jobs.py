"""Background job APIs (Phase 11)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from services.jobs import list_jobs, run_due_jobs

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
def get_jobs(
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """List background jobs (transparency: what the scheduler is doing)."""
    valid = ("PENDING", "DONE", "FAILED")
    if status is not None and status not in valid:
        raise HTTPException(status_code=422, detail=f"unknown status {status}; expected one of {valid}")
    return list_jobs(db, status=status, limit=limit)


class RunJobsRequest(BaseModel):
    force: bool = False


@router.post("/run")
def run_jobs(body: RunJobsRequest | None = None, db: Session = Depends(get_db)) -> dict:
    """Execute due background jobs now (deterministic trigger for tests/demo).

    With `force: true` every PENDING job runs regardless of its due time; the
    scheduler tick executes the exact same function.
    """
    force = bool(body and body.force)
    return run_due_jobs(db, force=force)
