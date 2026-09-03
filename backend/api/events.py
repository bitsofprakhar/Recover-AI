from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from models import PaymentEvent
from services.event_intake import MalformedEventError, build_envelope, process_envelope

router = APIRouter(prefix="/api/events", tags=["events"])


class EventSpecRequest(BaseModel):
    payment_id: str
    event: str
    amount_paise: int | None = None
    method: str | None = None
    order_id: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    created_at: int | None = None


def _submit(spec: EventSpecRequest, db: Session, source: str) -> dict:
    try:
        envelope = build_envelope(db, spec.model_dump())
        return process_envelope(db, envelope, source)
    except MalformedEventError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/synthetic")
def create_synthetic_event(spec: EventSpecRequest, db: Session = Depends(get_db)) -> dict:
    return _submit(spec, db, "SYNTHETIC")


@router.post("/replay")
def replay_event(spec: EventSpecRequest, db: Session = Depends(get_db)) -> dict:
    return _submit(spec, db, "REPLAY")


@router.get("")
def list_events(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(PaymentEvent).order_by(PaymentEvent.id.desc())
    total = query.count()
    events = query.offset(offset).limit(limit).all()
    return {"total": total, "items": [_serialize(event) for event in events]}


@router.get("/{event_id}")
def get_event(event_id: str, db: Session = Depends(get_db)) -> dict:
    event = db.query(PaymentEvent).filter(PaymentEvent.event_id == event_id).one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    data = _serialize(event)
    data["raw_payload"] = event.raw_payload
    return data


def _serialize(event: PaymentEvent) -> dict:
    return {
        "event_id": event.event_id,
        "source": event.source,
        "event_type": event.event_type,
        "payment_ref": event.payment_ref,
        "entity_status": event.entity_status,
        "processing_status": event.processing_status,
        "reason": event.reason,
        "received_at": event.received_at.isoformat(),
        "processed_at": event.processed_at.isoformat() if event.processed_at else None,
    }
