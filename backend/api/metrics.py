"""Recovery metrics API (Phase 9, README Section 11)."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db import get_db
from services.metrics import compute_metrics

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> dict:
    """Recovery metrics computed from stored cases, actions and audit rows."""
    return compute_metrics(db)
