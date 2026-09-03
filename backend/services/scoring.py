"""Recovery scoring and decision policy (Phase 6, README rules 3-6).

Deterministic, explainable 0-100 weighted score over the six README inputs:
failure reason, transaction amount, customer success history, previous
recovery attempts (customer lifetime + this case), payment method and
recency. The score is decision support, not proof of success: thresholds
translate it into a band (HIGH / MEDIUM / LOW), and the decision policy
combines the band with the agent's recommendation to select the action.
Every factor, weight and point contribution is returned so a judge can see
exactly why an action was selected.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from config import settings
from models import RecoveryCase

BAND_HIGH = "HIGH"
BAND_MEDIUM = "MEDIUM"
BAND_LOW = "LOW"

FAILURE_REASON_FACTORS = {
    "INSUFFICIENT_FUNDS": 1.0,
    "BANK_TIMEOUT": 0.8,
    "NETWORK_ERROR": 0.8,
    "AUTHENTICATION_FAILED": 0.5,
    "LIMIT_EXCEEDED": 0.5,
    "CARD_DECLINED": 0.4,
    "INVALID_VPA": 0.3,
    "RISK_BLOCKED": 0.0,
}
DEFAULT_FAILURE_REASON_FACTOR = 0.5

METHOD_FACTORS = {"UPI": 0.9, "CARD": 0.8, "NETBANKING": 0.7, "WALLET": 0.6}
DEFAULT_METHOD_FACTOR = 0.5

WEIGHTS = {
    "failure_reason": 25,
    "customer_success_rate": 30,
    "method": 10,
    "amount": 10,
    "prior_attempts": 10,
    "recency": 15,
}


def _failure_reason_factor(reason: str | None) -> float:
    return FAILURE_REASON_FACTORS.get(reason or "", DEFAULT_FAILURE_REASON_FACTOR)


def _method_factor(method: str | None) -> float:
    return METHOD_FACTORS.get(method or "", DEFAULT_METHOD_FACTOR)


def _amount_factor(amount) -> float:
    if amount is None:
        return 0.5
    value = Decimal(amount)
    if value <= 500:
        return 1.0
    if value <= 2000:
        return 0.8
    if value <= 10000:
        return 0.6
    return 0.4


def _prior_attempts_factor(customer_prior: int, case_attempts: int) -> float:
    effective = max(customer_prior, 0) + max(case_attempts, 0)
    return max(0.0, 1.0 - 0.2 * effective)


def _recency_factor(updated_at: datetime) -> float:
    hours = max((datetime.now(timezone.utc) - updated_at).total_seconds() / 3600, 0.0)
    return max(0.2, 1.0 - 0.8 * min(hours, 24.0) / 24.0)


def band_for_score(score: int) -> str:
    if score >= settings.score_high_threshold:
        return BAND_HIGH
    if score >= settings.score_stop_threshold:
        return BAND_MEDIUM
    return BAND_LOW


def compute_score(db: Session, case: RecoveryCase) -> dict:
    payment = case.payment
    order = payment.order if payment is not None else None
    customer = order.customer if order is not None else None

    success_rate = (
        round(customer.lifetime_successes / customer.lifetime_payments, 4)
        if customer is not None and customer.lifetime_payments
        else 0.0
    )
    hours = (
        round(max((datetime.now(timezone.utc) - payment.updated_at).total_seconds() / 3600, 0.0), 2)
        if payment is not None
        else None
    )

    factor_values = {
        "failure_reason": _failure_reason_factor(payment.failure_reason if payment is not None else None),
        "customer_success_rate": success_rate,
        "method": _method_factor(payment.method if payment is not None else None),
        "amount": _amount_factor(payment.amount if payment is not None else None),
        "prior_attempts": _prior_attempts_factor(
            customer.prior_recovery_attempts if customer is not None else 0,
            case.attempt_count,
        ),
        "recency": _recency_factor(payment.updated_at) if payment is not None else 0.2,
    }
    inputs = {
        "failure_reason": payment.failure_reason if payment is not None else None,
        "amount": str(payment.amount) if payment is not None else None,
        "method": payment.method if payment is not None else None,
        "customer_success_rate": success_rate,
        "customer_prior_recovery_attempts": customer.prior_recovery_attempts if customer is not None else None,
        "case_attempt_count": case.attempt_count,
        "hours_since_last_update": hours,
    }

    factor_inputs = {
        "failure_reason": inputs["failure_reason"],
        "customer_success_rate": success_rate,
        "method": inputs["method"],
        "amount": inputs["amount"],
        "prior_attempts": {
            "customer_prior_recovery_attempts": inputs["customer_prior_recovery_attempts"],
            "case_attempt_count": inputs["case_attempt_count"],
        },
        "recency": {"hours_since_last_update": hours},
    }

    factors = []
    total = 0.0
    for name, weight in WEIGHTS.items():
        factor = factor_values[name]
        points = weight * factor
        total += points
        factors.append(
            {
                "name": name,
                "input": factor_inputs[name],
                "factor": round(factor, 4),
                "weight": weight,
                "points": round(points, 2),
            }
        )

    score = max(0, min(100, round(total)))
    return {
        "score": score,
        "band": band_for_score(score),
        "thresholds": {
            "high": settings.score_high_threshold,
            "stop": settings.score_stop_threshold,
        },
        "factors": factors,
        "inputs": inputs,
    }


def decide_action(band: str, recommended_action: str) -> dict:
    if recommended_action == "ESCALATE":
        return {
            "selected_action": "ESCALATE",
            "decision": "ESCALATE",
            "reason": "agent recommended escalation; the score is not actioned",
        }
    if band == BAND_LOW:
        return {
            "selected_action": None,
            "decision": "STOP",
            "reason": f"score is below the stop threshold ({settings.score_stop_threshold}); no recovery action is selected",
        }
    if band == BAND_MEDIUM and recommended_action == "RETRY_PAYMENT_LINK":
        return {
            "selected_action": "SEND_NOTIFICATION_ONLY",
            "decision": "CAUTIOUS",
            "reason": "medium band: full retry downgraded to the cautious notification-only action",
        }
    decision = "PROCEED" if band == BAND_HIGH else "CAUTIOUS"
    reason = (
        "high band: the agent's recommended action is eligible for recovery"
        if band == BAND_HIGH
        else "medium band: the cautious recommended action is kept as selected"
    )
    return {
        "selected_action": recommended_action,
        "decision": decision,
        "reason": reason,
    }
