"""Shared audit-trail writer: every decision and state change is recorded."""
from sqlalchemy.orm import Session

from models import AuditLog


def _status_value(status):
    return status.value if hasattr(status, "value") else status


def record(
    db: Session,
    event_type: str,
    payload: dict | None = None,
    case_id: int | None = None,
    from_status=None,
    to_status=None,
) -> None:
    db.add(
        AuditLog(
            event_type=event_type,
            case_id=case_id,
            from_status=_status_value(from_status),
            to_status=_status_value(to_status),
            payload=payload,
        )
    )
