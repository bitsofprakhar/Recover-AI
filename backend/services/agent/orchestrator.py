"""Agent orchestration: diagnosis, scoring, decision and safety gate (Phases 5-7).

run_agent moves a DETECTED case to DIAGNOSING, assembles and validates context
through the backend tool layer, produces a structured diagnosis, runs the
Phase 6 scoring pipeline (DIAGNOSING -> SCORED -> ACTION_SELECTED / STOPPED /
escalation) and, when an action is selected, submits it to the Phase 7 safety
gate (ACTION_SELECTED -> SAFETY_CHECK). The gate's ALLOW / BLOCK / ESCALATE
decision is applied deterministically: ALLOW parks the case in SAFETY_CHECK
for the Phase 8 executor, BLOCK may re-select a safe alternative (rule 9,
bounded by the re-selection budget) and anything ambiguous escalates (rules
10/17). Missing or conflicting context escalates the case (rule 4) instead of
guessing. Act tools stay blocked until Phase 8: the LLM can only recommend.
"""
import json

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from config import settings
from models import TERMINAL_CASE_STATUSES, AgentAction, RecoveryCase, RecoveryCaseStatus
from services.audit import record
from services.case_lifecycle import transition
from services import scoring
from services.safety_gate import GATE_TOOL_NAME as GATE_TOOL
from services.safety_gate import submit_to_gate

from . import llm
from .context import build_context
from .tools import APPROVED_ACTIONS, execute_tool, tool_schemas


class CaseNotFoundError(Exception):
    pass


class DiagnosisSubmission(BaseModel):
    failure_analysis: str = Field(min_length=1)
    customer_assessment: str = ""
    recovery_strategy: str = ""
    recommended_action: str
    recommendation_reasoning: str = ""
    confidence: str = "MEDIUM"
    escalate_reason: str | None = None


SUBMIT_DIAGNOSIS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_diagnosis",
        "description": "Submit the final structured diagnosis and recovery recommendation for the case.",
        "parameters": {
            "type": "object",
            "properties": {
                "failure_analysis": {"type": "string", "description": "why the payment failed and what it means for recovery"},
                "customer_assessment": {"type": "string", "description": "customer payment history and behaviour signals"},
                "recovery_strategy": {"type": "string", "description": "the strategy behind the recommended action"},
                "recommended_action": {"type": "string", "enum": list(APPROVED_ACTIONS)},
                "recommendation_reasoning": {"type": "string", "description": "why this action was chosen"},
                "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                "escalate_reason": {"type": "string", "description": "required when recommended_action is ESCALATE"},
            },
            "required": ["failure_analysis", "recommended_action", "recommendation_reasoning", "confidence"],
        },
    },
}

DIAGNOSABLE_STATUSES = (RecoveryCaseStatus.DETECTED, RecoveryCaseStatus.DIAGNOSING)


def run_agent(db: Session, case_id: int) -> dict:
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).one_or_none()
    if case is None:
        raise CaseNotFoundError(f"case {case_id} not found")

    if case.status not in DIAGNOSABLE_STATUSES:
        if case.status in TERMINAL_CASE_STATUSES:
            reason = f"case is terminal ({case.status.value}); no agent run is possible"
        else:
            reason = f"agent runs only on DETECTED or DIAGNOSING cases (current: {case.status.value})"
        return {
            "case_id": case.id,
            "decision": "NOOP",
            "case_status": case.status.value,
            "reason": reason,
            "diagnosis": None,
            "context_assessment": None,
            "score": None,
            "selection": None,
            "gate": None,
            "tool_calls": [],
        }

    if case.status == RecoveryCaseStatus.DETECTED:
        transition(db, case, RecoveryCaseStatus.DIAGNOSING, "agent.diagnosis_started", payload={"case_id": case.id})
        db.flush()

    context, tool_calls = build_context(db, case)
    assessment = context["assessment"]

    if assessment["ambiguities"]:
        diagnosis = _escalation_diagnosis(assessment["ambiguities"])
        _store_diagnosis(db, case, diagnosis)
        transition(
            db,
            case,
            RecoveryCaseStatus.ESCALATED,
            "agent.case_escalated",
            payload={"reasons": assessment["ambiguities"]},
        )
        db.commit()
        return {
            "case_id": case.id,
            "decision": "ESCALATED",
            "case_status": case.status.value,
            "diagnosis": diagnosis,
            "context_assessment": assessment,
            "score": None,
            "selection": None,
            "gate": None,
            "tool_calls": tool_calls,
        }

    if llm.is_configured():
        diagnosis, llm_calls, fallback_note = _llm_diagnosis(db, case, context)
        tool_calls = tool_calls + llm_calls
        if diagnosis is None:
            diagnosis = _fallback_diagnosis(context)
            diagnosis["reasoning_source"] = "rule_based_fallback"
            diagnosis["fallback_note"] = fallback_note
    else:
        diagnosis = _fallback_diagnosis(context)
        diagnosis["reasoning_source"] = "rule_based_fallback"
        diagnosis["fallback_note"] = (
            "GLM structured tool calling is not configured (set AGENT_LLM_BASE_URL and "
            "AGENT_LLM_API_KEY); deterministic rule-based diagnosis used"
        )

    _store_diagnosis(db, case, diagnosis)
    record(
        db,
        "agent.diagnosis_completed",
        case_id=case.id,
        from_status=RecoveryCaseStatus.DIAGNOSING,
        payload={
            "recommended_action": diagnosis["recommended_action"],
            "reasoning_source": diagnosis["reasoning_source"],
        },
    )

    score_out, selection = _apply_decision_policy(db, case, diagnosis)
    tool_calls = tool_calls + [{"tool_name": score_out["tool"], "status": score_out["status"]}]

    gate_result = None
    if case.status == RecoveryCaseStatus.ACTION_SELECTED:
        submissions = 0
        while case.status == RecoveryCaseStatus.ACTION_SELECTED and submissions <= settings.gate_reselection_budget:
            gate_result = submit_to_gate(db, case)
            if gate_result["decision"] == "NOOP":
                break
            if gate_result.get("gate"):
                tool_calls.append(
                    {"tool_name": GATE_TOOL, "status": gate_result["gate"].get("decision", "NOOP")}
                )
            submissions += 1

    db.commit()
    return {
        "case_id": case.id,
        "decision": case.status.value,
        "case_status": case.status.value,
        "diagnosis": diagnosis,
        "context_assessment": assessment,
        "score": {
            "score": case.score,
            "band": score_out.get("band"),
            "thresholds": score_out.get("thresholds"),
            "factors": score_out.get("factors"),
        },
        "selection": selection,
        "gate": gate_result,
        "tool_calls": tool_calls,
    }


def _apply_decision_policy(db: Session, case: RecoveryCase, diagnosis: dict) -> tuple[dict, dict]:
    score_out = execute_tool(db, case, "calculate_recovery_score", {})
    score = score_out.get("score") if isinstance(score_out, dict) else None
    if not isinstance(score, int):
        selection = {
            "selected_action": "ESCALATE",
            "decision": "ESCALATE",
            "reason": "score computation failed; the case cannot be safely actioned",
        }
        _transition_selected(db, case, RecoveryCaseStatus.ESCALATED, "agent.case_escalated", score_out, selection)
        return score_out, selection

    case.score = score
    band = score_out.get("band")
    transition(
        db,
        case,
        RecoveryCaseStatus.SCORED,
        "agent.scored",
        payload={"score": score, "band": band, "thresholds": score_out.get("thresholds")},
    )

    selection = scoring.decide_action(band, diagnosis["recommended_action"])
    if selection["decision"] == "ESCALATE":
        _transition_selected(db, case, RecoveryCaseStatus.ESCALATED, "agent.case_escalated", score_out, selection)
    elif selection["decision"] == "STOP":
        transition(
            db,
            case,
            RecoveryCaseStatus.STOPPED,
            "agent.case_stopped",
            payload={"score": score, "band": band, "reason": selection["reason"]},
        )
    else:
        case.selected_action = selection["selected_action"]
        transition(
            db,
            case,
            RecoveryCaseStatus.ACTION_SELECTED,
            "agent.action_selected",
            payload={
                "score": score,
                "band": band,
                "selected_action": selection["selected_action"],
                "decision": selection["decision"],
                "reason": selection["reason"],
            },
        )
    return score_out, selection


def _transition_selected(
    db: Session, case: RecoveryCase, to_status: RecoveryCaseStatus, event_type: str, score_out: dict, selection: dict
) -> None:
    case.selected_action = selection["selected_action"]
    transition(
        db,
        case,
        to_status,
        event_type,
        payload={
            "score": case.score,
            "band": score_out.get("band"),
            "reason": selection["reason"],
        },
    )


def _store_diagnosis(db: Session, case: RecoveryCase, diagnosis: dict) -> None:
    case.diagnosis = json.dumps(diagnosis, indent=2, default=str)
    db.add(
        AgentAction(
            case_id=case.id,
            tool_name="submit_diagnosis",
            input={"reasoning_source": diagnosis.get("reasoning_source")},
            output=diagnosis,
            allowed=True,
        )
    )
    db.flush()


def _escalation_diagnosis(reasons: list[str]) -> dict:
    return {
        "failure_analysis": "Required diagnostic context is missing or conflicting.",
        "customer_assessment": "Not assessed: context incomplete.",
        "recovery_strategy": "Escalate to human review; no recovery action is selected on ambiguous context.",
        "recommended_action": "ESCALATE",
        "recommendation_reasoning": "Ambiguity triggers: " + ", ".join(reasons),
        "confidence": "HIGH",
        "escalate_reason": ", ".join(reasons),
        "reasoning_source": "deterministic_context_assessment",
    }


def _fallback_diagnosis(context: dict) -> dict:
    payment = context["payment"]
    customer = context["customer"]
    reason = payment.get("failure_reason") or "UNKNOWN"
    method = payment.get("method") or "UNKNOWN"
    amount = payment.get("amount") or context["case"]["revenue_at_risk"]
    failure_analysis = (
        f"{method} payment of INR {amount} failed with failure reason {reason} "
        f"(case revenue at risk: INR {context['case']['revenue_at_risk']})."
    )
    if customer.get("found"):
        rate = customer.get("success_rate") or 0.0
        customer_assessment = (
            f"Customer {customer['customer_id']} has {customer['lifetime_successes']} successful payments "
            f"out of {customer['lifetime_payments']} (success rate {rate:.0%}); prior recovery attempts "
            f"{customer['prior_recovery_attempts']}, successes {customer['prior_recovery_successes']}."
        )
    else:
        rate = 0.0
        customer_assessment = "No customer history available."

    if reason == "RISK_BLOCKED":
        recommended = "ESCALATE"
        confidence = "HIGH"
        strategy = "The gateway blocked this payment for risk reasons; a human must review before any recovery attempt."
        escalate_reason = "Gateway RISK_BLOCKED failure requires human review before recovery."
    else:
        escalate_reason = None
        if rate >= 0.5:
            recommended = "RETRY_PAYMENT_LINK"
            confidence = "HIGH"
            strategy = "Strong payment history: create a recovery payment link so the customer can complete the purchase."
        elif rate >= 0.25:
            recommended = "SEND_NOTIFICATION_ONLY"
            confidence = "MEDIUM"
            strategy = "Mixed payment history: send a recovery notification first and retry only if the customer responds."
        else:
            recommended = "WAIT_AND_MONITOR"
            confidence = "LOW"
            strategy = "Weak payment history: take no immediate action and monitor for an independent customer retry."

    return {
        "failure_analysis": failure_analysis,
        "customer_assessment": customer_assessment,
        "recovery_strategy": strategy,
        "recommended_action": recommended,
        "recommendation_reasoning": f"failure reason {reason}; customer success rate {rate:.0%}",
        "confidence": confidence,
        "escalate_reason": escalate_reason,
    }


def _validate_submission(raw: dict) -> dict | None:
    try:
        submission = DiagnosisSubmission(**raw)
    except ValidationError:
        return None
    data = submission.model_dump()
    if data["recommended_action"] not in APPROVED_ACTIONS:
        return None
    data["confidence"] = (data["confidence"] or "").upper()
    if data["confidence"] not in ("HIGH", "MEDIUM", "LOW"):
        return None
    if data["recommended_action"] == "ESCALATE" and not (data.get("escalate_reason") or "").strip():
        return None
    return data


def _llm_diagnosis(db: Session, case: RecoveryCase, context: dict) -> tuple[dict | None, list[dict], str | None]:
    messages = llm.build_messages(context)
    tools = tool_schemas() + [SUBMIT_DIAGNOSIS_SCHEMA]
    calls: list[dict] = []

    for _ in range(max(1, settings.agent_max_tool_calls)):
        try:
            message = llm.chat(messages, tools)
        except llm.LLMError as exc:
            return None, calls, f"GLM call failed: {exc}"

        message_tool_calls = message.get("tool_calls") or []
        if not message_tool_calls:
            messages.append({"role": "assistant", "content": message.get("content") or ""})
            messages.append(
                {"role": "user", "content": "Call submit_diagnosis with your structured diagnosis now."}
            )
            continue

        messages.append(message)
        submitted: dict | None = None
        for call in message_tool_calls:
            function = call.get("function", {})
            name = function.get("name", "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                arguments = None
            if name == "submit_diagnosis":
                submitted = arguments if isinstance(arguments, dict) else {}
                continue
            output = execute_tool(db, case, name, arguments if isinstance(arguments, dict) else {})
            calls.append({"tool_name": name, "status": output.get("status")})
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(output, default=str)}
            )

        if submitted is not None:
            validated = _validate_submission(submitted)
            if validated is not None:
                validated["reasoning_source"] = settings.agent_llm_model
                return validated, calls, None
            return None, calls, "GLM submitted a diagnosis that failed backend validation"

    return None, calls, "GLM did not submit a diagnosis within the tool-call budget"
