"""Recovery action executor (Phase 8, README rules 8 and 11).

Executes only actions the Phase 7 safety gate has ALLOWed: a case must be in
SAFETY_CHECK, and the gate is re-verified at execution time (the world may
have changed since the ALLOW). Execution is simulated end to end - a recovery
payment/link row and a recorded notification, never a real payment operation
or delivery. Each execution increments the attempt counter for act actions,
moves the case SAFETY_CHECK -> ACTION_EXECUTED -> WAITING_FOR_RESULT through
the audited state machine, and is idempotent: re-executing a waiting case
replays the recorded execution without creating anything new. Revenue is
never marked recovered here - verification and attribution are Phase 9.

Phase 11 addition: a successful execution schedules the delayed verification
job (verify:{case}:{attempt}) so monitoring continues asynchronously.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from config import settings
from models import TERMINAL_CASE_STATUSES, AgentAction, RecoveryCase, RecoveryCaseStatus
from services.agent.tools import execute_tool
from services.case_lifecycle import transition
from services.safety_gate import DECISION_ALLOW, apply_verdict, evaluate

EXECUTOR_TOOL_NAME = "execute_recovery_action"
ACT_ACTIONS = ("RETRY_PAYMENT_LINK", "SEND_NOTIFICATION_ONLY")
MONITOR_ACTION = "WAIT_AND_MONITOR"

EXECUTED_STATUSES = (RecoveryCaseStatus.ACTION_EXECUTED, RecoveryCaseStatus.WAITING_FOR_RESULT)


def _recorded_execution(db: Session, case: RecoveryCase) -> dict | None:
    rows = (
        db.query(AgentAction)
        .filter(AgentAction.case_id == case.id, AgentAction.tool_name == EXECUTOR_TOOL_NAME)
        .order_by(AgentAction.id)
        .all()
    )
    if not rows:
        return None
    latest = rows[-1]
    output = dict(latest.output or {})
    output["replay"] = True
    return output


def execute_action(db: Session, case: RecoveryCase) -> dict:
    if case.status in TERMINAL_CASE_STATUSES:
        return {
            "case_id": case.id,
            "decision": "NOOP",
            "case_status": case.status.value,
            "reason": f"case is terminal ({case.status.value}); no recovery action can execute",
            "execution": None,
        }
    if case.status in EXECUTED_STATUSES:
        return {
            "case_id": case.id,
            "decision": "WAITING_FOR_RESULT",
            "case_status": case.status.value,
            "reason": "action already executed; the case is waiting for its outcome",
            "execution": _recorded_execution(db, case),
        }
    if case.status != RecoveryCaseStatus.SAFETY_CHECK:
        return {
            "case_id": case.id,
            "decision": "NOOP",
            "case_status": case.status.value,
            "reason": f"execution requires a gate-ALLOWed case in SAFETY_CHECK (current: {case.status.value})",
            "execution": None,
        }

    action = case.selected_action
    attempt = case.attempt_count + 1
    key = f"exec:{case.id}:{action}:{attempt}"

    verdict = evaluate(db, case)
    if verdict["decision"] != DECISION_ALLOW:
        verdict["idempotency_key"] = key
        verdict["recheck"] = True
        db.add(
            AgentAction(
                case_id=case.id,
                tool_name="safety_gate",
                input={"idempotency_key": key, "action": action, "recheck": True},
                output=verdict,
                allowed=False,
            )
        )
        db.flush()
        apply_verdict(db, case, verdict)
        db.commit()
        return {
            "case_id": case.id,
            "decision": case.status.value,
            "case_status": case.status.value,
            "reason": f"gate re-verification at execution time returned {verdict['decision']} ({verdict['reason']}); nothing executed",
            "execution": None,
        }

    tool_calls: list[dict] = []
    recovery_payment_out = None
    notification_out = None

    if action in ACT_ACTIONS:
        if action == "RETRY_PAYMENT_LINK":
            recovery_payment_out = execute_tool(db, case, "create_recovery_payment", {}, authorized=True)
            tool_calls.append({"tool_name": "create_recovery_payment", "status": recovery_payment_out["status"]})
        notification_out = execute_tool(
            db,
            case,
            "send_recovery_notification",
            {"channel": settings.default_notification_channel},
            authorized=True,
        )
        tool_calls.append({"tool_name": "send_recovery_notification", "status": notification_out["status"]})

        failed = [call for call in tool_calls if call["status"] != "OK"]
        if failed:
            transition(
                db,
                case,
                RecoveryCaseStatus.ESCALATED,
                "action.execution_failed",
                payload={"action": action, "failed_tools": failed, "idempotency_key": key},
            )
            db.commit()
            return {
                "case_id": case.id,
                "decision": "ESCALATED",
                "case_status": case.status.value,
                "reason": f"simulated execution failed for {failed}; nothing further executed",
                "execution": None,
            }

        case.attempt_count = attempt
    else:
        tool_calls.append({"tool_name": "monitor", "status": "OK"})

    simulated = True
    transition(
        db,
        case,
        RecoveryCaseStatus.ACTION_EXECUTED,
        "action.executed",
        payload={
            "action": action,
            "attempt_count": case.attempt_count,
            "simulated": simulated,
            "recovery_payment_id": (recovery_payment_out or {}).get("payment_id"),
            "recovery_link_id": (recovery_payment_out or {}).get("link_id"),
            "notification_channel": (notification_out or {}).get("channel"),
            "idempotency_key": key,
        },
    )
    transition(
        db,
        case,
        RecoveryCaseStatus.WAITING_FOR_RESULT,
        "action.monitoring_started",
        payload={
            "action": action,
            "attempt_count": case.attempt_count,
            "simulated": simulated,
            "idempotency_key": key,
        },
    )

    from services.jobs import schedule_job, verify_job_key

    schedule_job(
        db,
        "verify_outcome",
        {"case_id": case.id},
        datetime.now(timezone.utc) + timedelta(seconds=settings.verification_delay_seconds),
        verify_job_key(case.id, case.attempt_count, "executed"),
    )

    execution = {
        "tool": EXECUTOR_TOOL_NAME,
        "status": "OK",
        "executed": True,
        "simulated": simulated,
        "action": action,
        "attempt_count": case.attempt_count,
        "recovery_payment_id": (recovery_payment_out or {}).get("payment_id"),
        "recovery_link_id": (recovery_payment_out or {}).get("link_id"),
        "notification_channel": (notification_out or {}).get("channel"),
        "tool_calls": tool_calls,
        "idempotency_key": key,
        "note": "simulated recovery action executed after gate ALLOW; revenue is credited only after Phase 9 verification and attribution (POST /api/cases/{id}/verify)",
    }
    db.add(
        AgentAction(
            case_id=case.id,
            tool_name=EXECUTOR_TOOL_NAME,
            input={"idempotency_key": key, "action": action},
            output=execution,
            allowed=True,
        )
    )
    db.flush()
    db.commit()
    return {
        "case_id": case.id,
        "decision": "WAITING_FOR_RESULT",
        "case_status": case.status.value,
        "execution": execution,
    }
