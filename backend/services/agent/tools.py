"""Controlled backend tools for the recovery agent (README Section 7).

Every tool the LLM can see is defined here with an explicit JSON schema and a
deterministic backend handler. The LLM selects tools; this layer decides what
exists, validates arguments server-side, and blocks gated act tools unless the
call comes from the Phase 8 action executor with a fresh Phase 7 gate ALLOW
(the `authorized` flag - only the executor sets it). Act tool execution is
simulated end to end: recovery payments are simulated payment rows and
notifications are recorded, never delivered. Every call - allowed, blocked or
errored - is logged to agent_actions with its structured input and output.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from sqlalchemy.orm import Session

from models import (
    TERMINAL_CASE_STATUSES,
    AgentAction,
    Payment,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.audit import record
from services.scoring import compute_score
from services.safety_gate import APPROVED_ACTIONS

ACT_TOOLS = ("create_recovery_payment", "send_recovery_notification")
NOTIFICATION_CHANNELS = ("EMAIL", "SMS", "WHATSAPP")
GATE_AUTHORIZATION_REQUIRED_REASON = "GATE_AUTHORIZATION_REQUIRED"

KIND_READ = "READ"
KIND_COMPUTE = "COMPUTE"
KIND_ACT = "ACT"
KIND_RECORD = "RECORD"


def jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and hasattr(value, "name"):
        return value.value
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict
    kind: str
    handler: Callable[[Session, RecoveryCase, dict], dict] | None


def _payment(db: Session, case: RecoveryCase) -> Payment:
    return db.query(Payment).filter(Payment.id == case.payment_id).one()


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain or not local:
        return email
    shown = local[:2] if len(local) >= 2 else local[0]
    return f"{shown}{'*' * max(len(local) - len(shown), 2)}@{domain}"


def _mask_phone(phone: str) -> str:
    if len(phone) <= 4:
        return phone
    return "*" * (len(phone) - 4) + phone[-4:]


def _get_payment_status(db: Session, case: RecoveryCase, args: dict) -> dict:
    payment = _payment(db, case)
    metadata = payment.gateway_metadata or {}
    return {
        "tool": "get_payment_status",
        "status": "OK",
        "payment_id": payment.payment_id,
        "payment_status": payment.status.value,
        "amount": payment.amount,
        "method": payment.method,
        "failure_reason": payment.failure_reason,
        "order_id": payment.order.order_id if payment.order is not None else None,
        "last_event_id": metadata.get("last_event_id"),
        "updated_at": payment.updated_at,
        "note": "current gateway-reported state; always checked before acting",
    }


def _get_order_details(db: Session, case: RecoveryCase, args: dict) -> dict:
    payment = _payment(db, case)
    order = payment.order
    if order is None:
        return {
            "tool": "get_order_details",
            "status": "OK",
            "found": False,
            "note": "no order is linked to this payment (missing order identity)",
        }
    return {
        "tool": "get_order_details",
        "status": "OK",
        "found": True,
        "order_id": order.order_id,
        "amount": order.amount,
        "currency": order.currency,
        "order_status": order.status.value,
        "customer_id": order.customer.customer_id,
        "created_at": order.created_at,
    }


def _get_customer_history(db: Session, case: RecoveryCase, args: dict) -> dict:
    payment = _payment(db, case)
    order = payment.order
    if order is None or order.customer is None:
        return {
            "tool": "get_customer_history",
            "status": "OK",
            "found": False,
            "note": "no order identity; customer history unavailable",
        }
    customer = order.customer
    history = db.query(Payment).filter(Payment.order_id == order.id).order_by(Payment.id).all()
    success_rate = (
        round(customer.lifetime_successes / customer.lifetime_payments, 4) if customer.lifetime_payments else 0.0
    )
    return {
        "tool": "get_customer_history",
        "status": "OK",
        "found": True,
        "customer_id": customer.customer_id,
        "name": customer.name,
        "email_masked": _mask_email(customer.email),
        "phone_masked": _mask_phone(customer.phone),
        "lifetime_payments": customer.lifetime_payments,
        "lifetime_successes": customer.lifetime_successes,
        "success_rate": success_rate,
        "prior_recovery_attempts": customer.prior_recovery_attempts,
        "prior_recovery_successes": customer.prior_recovery_successes,
        "order_payment_history": [
            {
                "payment_id": item.payment_id,
                "status": item.status.value,
                "amount": item.amount,
                "failure_reason": item.failure_reason,
                "updated_at": item.updated_at,
            }
            for item in history
        ],
    }


def _calculate_recovery_score(db: Session, case: RecoveryCase, args: dict) -> dict:
    result = compute_score(db, case)
    return {
        "tool": "calculate_recovery_score",
        "status": "OK",
        "score": result["score"],
        "band": result["band"],
        "thresholds": result["thresholds"],
        "factors": result["factors"],
        "inputs": result["inputs"],
        "note": "deterministic weighted score (0-100); every factor, weight and point contribution is returned so the decision is explainable",
    }


def _recovery_payment_id(case: RecoveryCase, attempt: int) -> str:
    return f"pay_rec_{case.id}_{attempt}"


def _find_recovery_payment(db: Session, case: RecoveryCase, attempt: int) -> Payment | None:
    return (
        db.query(Payment)
        .filter(Payment.payment_id == _recovery_payment_id(case, attempt))
        .one_or_none()
    )


def _create_recovery_payment(db: Session, case: RecoveryCase, args: dict) -> dict:
    """Create the simulated recovery payment/link (Phase 8 executor path).

    Only reachable with authorized=True from the action executor, after the
    Phase 7 gate ALLOWed the case's selected action. Idempotent per case and
    attempt: an existing recovery payment for this attempt is returned as is.
    """
    attempt = case.attempt_count + 1
    payment = _payment(db, case)
    amount = Decimal(str(args["amount"])) if args.get("amount") is not None else case.revenue_at_risk
    link_id = f"rlink_{case.id}_{attempt}"

    existing = _find_recovery_payment(db, case, attempt)
    if existing is not None:
        return {
            "tool": "create_recovery_payment",
            "status": "OK",
            "executed": True,
            "simulated": True,
            "payment_id": existing.payment_id,
            "link_id": (existing.gateway_metadata or {}).get("recovery_link_id"),
            "amount": str(existing.amount),
            "method": existing.method,
            "reused": True,
            "note": "simulated recovery payment/link already existed for this attempt; nothing new created",
        }

    row = Payment(
        payment_id=_recovery_payment_id(case, attempt),
        order_id=payment.order_id,
        amount=amount,
        method=payment.method,
        status=PaymentStatus.PENDING,
        gateway_metadata={
            "simulated": True,
            "recovery_case_id": case.id,
            "recovery_link_id": link_id,
            "created_by": "action_executor",
            "gateway": "razorpay",
            "mode": "test",
        },
    )
    db.add(row)
    db.flush()
    return {
        "tool": "create_recovery_payment",
        "status": "OK",
        "executed": True,
        "simulated": True,
        "payment_id": row.payment_id,
        "link_id": link_id,
        "amount": str(row.amount),
        "method": row.method,
        "order_id": row.order.order_id if row.order is not None else None,
        "note": "simulated recovery payment/link created for the customer to complete the purchase; no real payment operation is performed",
    }


def _send_recovery_notification(db: Session, case: RecoveryCase, args: dict) -> dict:
    """Record the simulated recovery notification (Phase 8 executor path).

    The notification is recorded with its channel and masked recipient; no
    email/SMS/WhatsApp is ever delivered.
    """
    channel = args.get("channel")
    payment = _payment(db, case)
    order = payment.order
    customer = order.customer if order is not None else None
    if customer is not None:
        recipient = _mask_email(customer.email) if channel == "EMAIL" else _mask_phone(customer.phone)
    else:
        recipient = None
    return {
        "tool": "send_recovery_notification",
        "status": "OK",
        "executed": True,
        "simulated": True,
        "channel": channel,
        "recipient_masked": recipient,
        "message": args.get("message"),
        "note": "simulated notification recorded; no real delivery is performed",
    }


def _check_recovery_result(db: Session, case: RecoveryCase, args: dict) -> dict:
    payment = _payment(db, case)
    order = payment.order
    act_actions = (
        db.query(AgentAction)
        .filter(AgentAction.case_id == case.id, AgentAction.tool_name.in_(ACT_TOOLS))
        .all()
    )
    executed = [action for action in act_actions if (action.output or {}).get("executed") is True]
    recovery_payment = None
    if order is not None:
        rows = db.query(Payment).filter(Payment.order_id == order.id).order_by(Payment.id).all()
        candidates = [row for row in rows if (row.gateway_metadata or {}).get("recovery_case_id") == case.id]
        if candidates:
            latest = candidates[-1]
            recovery_payment = {
                "payment_id": latest.payment_id,
                "status": latest.status.value,
                "amount": str(latest.amount),
                "simulated": bool((latest.gateway_metadata or {}).get("simulated")),
            }
    return {
        "tool": "check_recovery_result",
        "status": "OK",
        "payment_id": payment.payment_id,
        "payment_status": payment.status.value,
        "order_id": order.order_id if order is not None else None,
        "order_status": order.status.value if order is not None else None,
        "recovery_action_executed": bool(executed),
        "recovery_payment": recovery_payment,
        "case_recovered": case.status == RecoveryCaseStatus.RECOVERED,
        "recovered_payment_id": case.recovered_payment_id,
        "recovered_amount": str(case.recovered_amount) if case.recovered_amount is not None else None,
        "recovered_at": case.recovered_at.isoformat() if case.recovered_at is not None else None,
        "note": "current state of the source payment, the order and the simulated recovery payment created by the action executor; revenue is credited only by the Phase 9 verification service (POST /api/cases/{id}/verify) after attribution",
    }


def _log_action(db: Session, case: RecoveryCase, args: dict) -> dict:
    note = args.get("note")
    if not isinstance(note, str) or not note.strip():
        return {
            "tool": "log_action",
            "status": "ERROR",
            "error": "INVALID_ARGUMENTS",
            "detail": "note is required and must be a non-empty string",
        }
    record(
        db,
        "agent.note",
        case_id=case.id,
        payload={"note": note.strip()[:1000], "source": "agent_tool"},
    )
    return {
        "tool": "log_action",
        "status": "OK",
        "recorded": True,
        "audit_event_type": "agent.note",
    }


def _validate_act_arguments(name: str, args: dict) -> str | None:
    if name == "create_recovery_payment":
        amount = args.get("amount")
        if amount is not None and (not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0):
            return "amount must be a positive number when provided"
    if name == "send_recovery_notification":
        channel = args.get("channel")
        if channel not in NOTIFICATION_CHANNELS:
            return f"channel must be one of {list(NOTIFICATION_CHANNELS)}"
    return None


TOOL_CATALOG: dict[str, ToolDef] = {
    tool.name: tool
    for tool in [
        ToolDef(
            name="get_payment_status",
            description="Read the current payment state (status, amount, method, failure reason). Always checked before acting.",
            parameters={"type": "object", "properties": {}},
            kind=KIND_READ,
            handler=_get_payment_status,
        ),
        ToolDef(
            name="get_order_details",
            description="Read order identity, amount, currency and status for the payment underlying the case.",
            parameters={"type": "object", "properties": {}},
            kind=KIND_READ,
            handler=_get_order_details,
        ),
        ToolDef(
            name="get_customer_history",
            description="Read customer payment behaviour signals: lifetime payments and successes, success rate, prior recovery attempts and the payment history of this order. Contact details are masked.",
            parameters={"type": "object", "properties": {}},
            kind=KIND_READ,
            handler=_get_customer_history,
        ),
        ToolDef(
            name="calculate_recovery_score",
            description="Compute the deterministic 0-100 recovery likelihood score with its full explainable factor breakdown (failure reason, customer success rate, method, amount, prior attempts, recency) and the HIGH/MEDIUM/LOW band.",
            parameters={"type": "object", "properties": {}},
            kind=KIND_COMPUTE,
            handler=_calculate_recovery_score,
        ),
        ToolDef(
            name="create_recovery_payment",
            description="Create a simulated recovery payment/link for the customer to complete the purchase (gated act tool; executes only through the action executor after a backend safety-gate ALLOW).",
            parameters={
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "recovery amount; defaults to the case revenue at risk", "exclusiveMinimum": 0},
                    "note": {"type": "string", "description": "optional internal note for the recovery record"},
                },
            },
            kind=KIND_ACT,
            handler=_create_recovery_payment,
        ),
        ToolDef(
            name="send_recovery_notification",
            description="Send a simulated recovery notification to the customer (gated act tool; executes only through the action executor after a backend safety-gate ALLOW).",
            parameters={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": list(NOTIFICATION_CHANNELS)},
                    "message": {"type": "string", "description": "optional message body for the notification"},
                },
                "required": ["channel"],
            },
            kind=KIND_ACT,
            handler=_send_recovery_notification,
        ),
        ToolDef(
            name="check_recovery_result",
            description="Verify the latest payment/order state, the outcome of an executed recovery action, and whether the case has been verified and credited as recovered (Phase 9 attribution).",
            parameters={"type": "object", "properties": {}},
            kind=KIND_READ,
            handler=_check_recovery_result,
        ),
        ToolDef(
            name="log_action",
            description="Persist a decision note to the case audit trail.",
            parameters={
                "type": "object",
                "properties": {"note": {"type": "string", "description": "the note to record (max 1000 characters)"}},
                "required": ["note"],
            },
            kind=KIND_RECORD,
            handler=_log_action,
        ),
    ]
}


def tool_schemas() -> list[dict]:
    return [
        {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}}
        for tool in TOOL_CATALOG.values()
    ]


def _log_tool_call(db: Session, case: RecoveryCase, name: str, arguments: dict, output: dict, allowed: bool | None) -> None:
    db.add(
        AgentAction(
            case_id=case.id,
            tool_name=name,
            input=jsonable(arguments) if arguments else {},
            output=jsonable(output),
            allowed=allowed,
        )
    )
    db.flush()


def execute_tool(db: Session, case: RecoveryCase, name: str, arguments: dict | None, authorized: bool = False) -> dict:
    """Execute a controlled tool call.

    `authorized=True` is set only by the Phase 8 action executor after a fresh
    Phase 7 gate ALLOW; it is the sole path through which act tools execute.
    Every other caller (agent context assembly, the LLM loop) gets act tools
    blocked and logged.
    """
    tool = TOOL_CATALOG.get(name)
    if tool is None:
        output = {"tool": name, "status": "ERROR", "error": "UNKNOWN_TOOL"}
        _log_tool_call(db, case, name, arguments or {}, output, allowed=False)
        return output

    if not isinstance(arguments, dict):
        arguments = {}

    allowed: bool | None = None
    if tool.kind == KIND_ACT:
        allowed = False
        if case.status in TERMINAL_CASE_STATUSES:
            output = {
                "tool": name,
                "status": "BLOCKED",
                "executed": False,
                "reason": "TERMINAL_CASE",
                "note": "the case is terminal; no recovery action can execute",
            }
        else:
            problem = _validate_act_arguments(name, arguments)
            if problem is not None:
                output = {
                    "tool": name,
                    "status": "ERROR",
                    "error": "INVALID_ARGUMENTS",
                    "detail": problem,
                    "executed": False,
                }
            elif not authorized:
                output = {
                    "tool": name,
                    "status": "BLOCKED",
                    "executed": False,
                    "reason": GATE_AUTHORIZATION_REQUIRED_REASON,
                    "note": "act tools execute only through the action executor after a Phase 7 safety-gate ALLOW; recorded as a recommendation only",
                }
            else:
                output = jsonable(tool.handler(db, case, arguments))
                allowed = output.get("executed") is True
    else:
        output = jsonable(tool.handler(db, case, arguments))
        if tool.kind == KIND_RECORD:
            allowed = output.get("status") == "OK"

    _log_tool_call(db, case, name, arguments, output, allowed=allowed)
    return output
