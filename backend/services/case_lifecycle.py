"""Recovery case state machine enforcement (README Sections 8-9).

Central, deterministic transition validation shared by every service that
moves a case through its lifecycle. Only the legal transitions of the state
transition table are accepted; anything else raises IllegalTransitionError,
which callers must treat as an ambiguity trigger (escalation).
"""
from sqlalchemy.orm import Session

from models import TERMINAL_CASE_STATUSES, RecoveryCase, RecoveryCaseStatus
from services.audit import record


class IllegalTransitionError(Exception):
    pass


ALLOWED_TRANSITIONS: dict[RecoveryCaseStatus, frozenset[RecoveryCaseStatus]] = {
    RecoveryCaseStatus.DETECTED: frozenset(
        {RecoveryCaseStatus.DIAGNOSING, RecoveryCaseStatus.STOPPED, RecoveryCaseStatus.ESCALATED}
    ),
    RecoveryCaseStatus.DIAGNOSING: frozenset(
        {RecoveryCaseStatus.SCORED, RecoveryCaseStatus.STOPPED, RecoveryCaseStatus.ESCALATED}
    ),
    RecoveryCaseStatus.SCORED: frozenset(
        {RecoveryCaseStatus.ACTION_SELECTED, RecoveryCaseStatus.STOPPED, RecoveryCaseStatus.ESCALATED}
    ),
    RecoveryCaseStatus.ACTION_SELECTED: frozenset(
        {RecoveryCaseStatus.SAFETY_CHECK, RecoveryCaseStatus.STOPPED, RecoveryCaseStatus.ESCALATED}
    ),
    RecoveryCaseStatus.SAFETY_CHECK: frozenset(
        {
            RecoveryCaseStatus.ACTION_EXECUTED,
            RecoveryCaseStatus.ACTION_SELECTED,
            RecoveryCaseStatus.STOPPED,
            RecoveryCaseStatus.ESCALATED,
        }
    ),
    RecoveryCaseStatus.ACTION_EXECUTED: frozenset(
        {RecoveryCaseStatus.WAITING_FOR_RESULT, RecoveryCaseStatus.STOPPED, RecoveryCaseStatus.ESCALATED}
    ),
    RecoveryCaseStatus.WAITING_FOR_RESULT: frozenset(
        {
            RecoveryCaseStatus.RECOVERED,
            RecoveryCaseStatus.DIAGNOSING,
            RecoveryCaseStatus.NOT_RECOVERED,
            RecoveryCaseStatus.WAITING_FOR_RESULT,
            RecoveryCaseStatus.STOPPED,
            RecoveryCaseStatus.ESCALATED,
        }
    ),
    RecoveryCaseStatus.RECOVERED: frozenset(),
    RecoveryCaseStatus.NOT_RECOVERED: frozenset(),
    RecoveryCaseStatus.STOPPED: frozenset(),
    RecoveryCaseStatus.ESCALATED: frozenset(),
}


def transition(
    db: Session,
    case: RecoveryCase,
    to_status: RecoveryCaseStatus,
    event_type: str,
    payload: dict | None = None,
) -> RecoveryCase:
    if case.status in TERMINAL_CASE_STATUSES:
        raise IllegalTransitionError(
            f"case {case.id} is terminal ({case.status.value}); no transitions are allowed"
        )
    if to_status not in ALLOWED_TRANSITIONS[case.status]:
        raise IllegalTransitionError(f"unsupported state transition {case.status.value} -> {to_status.value}")
    previous = case.status
    case.status = to_status
    record(
        db,
        event_type,
        payload=payload,
        case_id=case.id,
        from_status=previous.value,
        to_status=to_status.value,
    )
    return case
