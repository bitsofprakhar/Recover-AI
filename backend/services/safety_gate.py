"""Safety / policy gate (Phase 7, README rules 7-10 and 16).

Every selected recovery action must pass deterministic backend validation
before anything executes. The gate re-verifies the original transaction
state, validates order identity and amount, blocks duplicate active
recovery cases, enforces the per-case attempt limit and the 24-hour case
window, and returns ALLOW / BLOCK / ESCALATE with a machine-readable
reason. BLOCK may carry a safe alternative action (rule 9, bounded by the
re-selection budget); ambiguity triggers escalate the case instead of
letting the AI invent an action. Every decision - allowed, blocked or
escalated - is logged to agent_actions and the audit trail, and repeated
submissions of the same action are idempotent by key.

Execution of ALLOWed actions is Phase 8: this gate only decides.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from config import settings
from models import (
    TERMINAL_CASE_STATUSES,
    AgentAction,
    AuditLog,
    OrderStatus,
    Payment,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.audit import record
from services.case_lifecycle import transition

APPROVED_ACTIONS = ("RETRY_PAYMENT_LINK", "SEND_NOTIFICATION_ONLY", "WAIT_AND_MONITOR", "ESCALATE")
ACTIONABLE_ACTIONS = ("RETRY_PAYMENT_LINK", "SEND_NOTIFICATION_ONLY", "WAIT_AND_MONITOR")
ACT_ACTIONS = ("RETRY_PAYMENT_LINK", "SEND_NOTIFICATION_ONLY")
MONITOR_ACTION = "WAIT_AND_MONITOR"

GATE_TOOL_NAME = "safety_gate"
DECISION_ALLOW = "ALLOW"
DECISION_BLOCK = "BLOCK"
DECISION_ESCALATE = "ESCALATE"

SUBMITTABLE_STATUSES = (RecoveryCaseStatus.ACTION_SELECTED, RecoveryCaseStatus.SAFETY_CHECK)


def _idempotency_key(case: RecoveryCase, action: str | None) -> str:
    return f"gate:{case.id}:{action}:{case.attempt_count}"


def _stored_decision(db: Session, case: RecoveryCase, key: str) -> dict | None:
    rows = (
        db.query(AgentAction)
        .filter(AgentAction.case_id == case.id, AgentAction.tool_name == GATE_TOOL_NAME)
        .order_by(AgentAction.id)
        .all()
    )
    for row in reversed(rows):
        if (row.input or {}).get("idempotency_key") == key:
            return row.output
    return None


def _reselections_used(db: Session, case: RecoveryCase) -> int:
    rows = db.query(AuditLog).filter(AuditLog.case_id == case.id, AuditLog.event_type == "gate.blocked").all()
    return sum(1 for row in rows if (row.payload or {}).get("alternative"))


def _decision(
    decision: str,
    reason: str,
    checks: list[dict],
    alternative: str | None = None,
    terminal: str | None = None,
) -> dict:
    out = {"tool": GATE_TOOL_NAME, "decision": decision, "reason": reason, "checks": checks}
    if alternative is not None:
        out["alternative"] = alternative
    if terminal is not None:
        out["terminal"] = terminal
    return out


def evaluate(db: Session, case: RecoveryCase) -> dict:
    """Pure evaluation: the decision and its checks, without state changes."""
    action = case.selected_action
    now = datetime.now(timezone.utc)
    checks: list[dict] = []

    if case.expiry <= now:
        checks.append({"name": "case_window", "result": "FAIL", "reason": "CASE_WINDOW_EXPIRED"})
        return _decision(DECISION_BLOCK, "CASE_WINDOW_EXPIRED", checks, terminal="STOPPED")
    checks.append({"name": "case_window", "result": "PASS"})

    if action not in ACTIONABLE_ACTIONS:
        checks.append({"name": "action_catalog", "result": "FAIL", "reason": "NO_VALID_ACTION"})
        return _decision(DECISION_ESCALATE, "NO_VALID_ACTION", checks)
    checks.append({"name": "action_catalog", "result": "PASS"})

    payment = db.query(Payment).filter(Payment.id == case.payment_id).one()
    order = payment.order

    if payment.status == PaymentStatus.CAPTURED:
        checks.append({"name": "payment_state", "result": "FAIL", "reason": "PAYMENT_ALREADY_SUCCESSFUL"})
        return _decision(DECISION_ESCALATE, "PAYMENT_ALREADY_SUCCESSFUL", checks)
    if payment.status != PaymentStatus.FAILED and action != MONITOR_ACTION:
        checks.append({"name": "payment_state", "result": "FAIL", "reason": "PAYMENT_NOT_FAILED"})
        return _decision(DECISION_BLOCK, "PAYMENT_NOT_FAILED", checks, alternative=MONITOR_ACTION)
    checks.append({"name": "payment_state", "result": "PASS"})

    if order is None:
        checks.append({"name": "order_identity", "result": "FAIL", "reason": "MISSING_ORDER_IDENTITY"})
        return _decision(DECISION_ESCALATE, "MISSING_ORDER_IDENTITY", checks)
    checks.append({"name": "order_identity", "result": "PASS"})

    if payment.amount != order.amount:
        checks.append({"name": "amount_match", "result": "FAIL", "reason": "AMOUNT_MISMATCH"})
        return _decision(DECISION_ESCALATE, "AMOUNT_MISMATCH", checks)
    checks.append({"name": "amount_match", "result": "PASS"})

    if order.status == OrderStatus.PAID:
        checks.append({"name": "order_state", "result": "FAIL", "reason": "CONFLICTING_ORDER_STATE"})
        return _decision(DECISION_ESCALATE, "CONFLICTING_ORDER_STATE", checks)
    checks.append({"name": "order_state", "result": "PASS"})

    duplicates = (
        db.query(RecoveryCase)
        .join(Payment, RecoveryCase.payment_id == Payment.id)
        .filter(
            Payment.order_id == order.id,
            RecoveryCase.id != case.id,
            ~RecoveryCase.status.in_(TERMINAL_CASE_STATUSES),
        )
        .count()
    )
    if duplicates:
        checks.append({"name": "duplicate_active_case", "result": "FAIL", "reason": "DUPLICATE_ACTIVE_CASE"})
        return _decision(DECISION_ESCALATE, "DUPLICATE_ACTIVE_CASE", checks)
    checks.append({"name": "duplicate_active_case", "result": "PASS"})

    if action in ACT_ACTIONS:
        if case.attempt_count >= settings.max_recovery_attempts:
            checks.append({"name": "attempt_limit", "result": "FAIL", "reason": "ATTEMPT_LIMIT_REACHED"})
            alternative = (
                MONITOR_ACTION if _reselections_used(db, case) < settings.gate_reselection_budget else None
            )
            return _decision(DECISION_BLOCK, "ATTEMPT_LIMIT_REACHED", checks, alternative=alternative)
        checks.append({"name": "attempt_limit", "result": "PASS"})
    else:
        checks.append(
            {"name": "attempt_limit", "result": "SKIP", "reason": "monitoring is not a recovery attempt"}
        )

    return _decision(DECISION_ALLOW, "ALL_CHECKS_PASSED", checks)


def apply_verdict(db: Session, case: RecoveryCase, result: dict) -> None:
    """Apply a gate verdict's outcome through the audited state machine.

    Used by submit_to_gate after the decision is logged, and by the Phase 8
    action executor when its execution-time re-verification returns something
    other than ALLOW. Transitions only; logging stays with the caller.
    """
    decision = result["decision"]
    key = result.get("idempotency_key")
    action = case.selected_action

    if decision == DECISION_ALLOW:
        record(
            db,
            "gate.allowed",
            case_id=case.id,
            from_status=RecoveryCaseStatus.SAFETY_CHECK,
            payload={"action": action, "reason": result["reason"], "idempotency_key": key},
        )
    elif decision == DECISION_BLOCK:
        reason = result["reason"]
        if result.get("terminal") == "STOPPED":
            transition(
                db,
                case,
                RecoveryCaseStatus.STOPPED,
                "gate.case_stopped",
                payload={"action": action, "reason": reason, "idempotency_key": key},
            )
        elif result.get("alternative"):
            alternative = result["alternative"]
            case.selected_action = alternative
            transition(
                db,
                case,
                RecoveryCaseStatus.ACTION_SELECTED,
                "gate.blocked",
                payload={
                    "action": action,
                    "reason": reason,
                    "alternative": alternative,
                    "idempotency_key": key,
                },
            )
        else:
            transition(
                db,
                case,
                RecoveryCaseStatus.ESCALATED,
                "gate.escalated",
                payload={"action": action, "reason": reason, "idempotency_key": key},
            )
    else:
        transition(
            db,
            case,
            RecoveryCaseStatus.ESCALATED,
            "gate.escalated",
            payload={"action": action, "reason": result["reason"], "idempotency_key": key},
        )


def submit_to_gate(db: Session, case: RecoveryCase) -> dict:
    """Submit the case's selected action to the gate and apply the outcome.

    Idempotent per submission key: re-submitting the same action of the same
    case returns the recorded decision without re-evaluating or re-logging.
    """
    if case.status in TERMINAL_CASE_STATUSES:
        return {
            "case_id": case.id,
            "decision": "NOOP",
            "case_status": case.status.value,
            "reason": f"case is terminal ({case.status.value}); the gate never evaluates a terminal case",
            "gate": None,
        }
    if case.status not in SUBMITTABLE_STATUSES:
        return {
            "case_id": case.id,
            "decision": "NOOP",
            "case_status": case.status.value,
            "reason": f"the gate accepts ACTION_SELECTED or SAFETY_CHECK cases (current: {case.status.value})",
            "gate": None,
        }

    action = case.selected_action
    key = _idempotency_key(case, action)

    if case.status == RecoveryCaseStatus.SAFETY_CHECK:
        stored = _stored_decision(db, case, key)
        if stored is not None:
            replay = dict(stored)
            replay["replay"] = True
            return {
                "case_id": case.id,
                "decision": stored.get("decision"),
                "case_status": case.status.value,
                "gate": replay,
            }
    else:
        transition(
            db,
            case,
            RecoveryCaseStatus.SAFETY_CHECK,
            "gate.submitted",
            payload={"action": action, "idempotency_key": key},
        )

    result = evaluate(db, case)
    result["idempotency_key"] = key
    decision = result["decision"]

    db.add(
        AgentAction(
            case_id=case.id,
            tool_name=GATE_TOOL_NAME,
            input={"idempotency_key": key, "action": action},
            output=result,
            allowed=decision == DECISION_ALLOW,
        )
    )
    db.flush()

    apply_verdict(db, case, result)

    db.flush()
    return {"case_id": case.id, "decision": decision, "case_status": case.status.value, "gate": result}
