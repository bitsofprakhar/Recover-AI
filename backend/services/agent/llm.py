"""GLM 5.3 structured tool-calling client (OpenAI-compatible chat completions).

Configured through AGENT_LLM_BASE_URL / AGENT_LLM_API_KEY / AGENT_LLM_MODEL.
When not configured the orchestrator uses its deterministic rule-based
fallback, so the loop never depends on external availability and tests and
offline demos stay deterministic. The model only ever recommends: every tool
call it requests is executed (or blocked) by the backend tool layer.
"""
import json

import httpx

from config import settings

from .tools import APPROVED_ACTIONS


class LLMError(Exception):
    pass


SYSTEM_PROMPT = f"""You are RecoverAI, an autonomous revenue-recovery agent for a merchant.
You are diagnosing one recovery case: a payment failed and expected revenue is at risk.

Operating rules (non-negotiable):
1. You recommend actions; the backend decides. Act tools (create_recovery_payment,
   send_recovery_notification) are validated by a backend safety gate before anything
   executes. You cannot bypass it, and blocked calls are still recorded.
2. Recommend only actions from the approved catalog: {", ".join(APPROVED_ACTIONS)}.
   Never invent actions, amounts or outcomes.
3. If context is missing, conflicting or uncertain, recommend ESCALATE with a reason.
   Never guess.
4. Pending payments are never recovered immediately: verification comes first.
5. Recovery outcomes are simulated and must be verified before revenue is counted.

Protocol:
- The user message contains the case context assembled and validated by the backend.
- You may call the read tools (get_payment_status, get_order_details,
  get_customer_history, check_recovery_result, calculate_recovery_score) to investigate,
  and you may attempt act tools to see the backend gate respond.
- When your analysis is complete, call submit_diagnosis exactly once with the
  structured diagnosis. This is the only way to deliver your result."""


def is_configured() -> bool:
    return bool(settings.agent_llm_base_url.strip() and settings.agent_llm_api_key.strip())


def build_messages(context: dict) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Case context (assembled and validated by the backend):\n\n"
                + json.dumps(context, indent=2, default=str)
                + "\n\nDiagnose this case and submit your structured diagnosis with the submit_diagnosis tool."
            ),
        },
    ]


def chat(messages: list[dict], tools: list[dict], tool_choice="auto") -> dict:
    url = settings.agent_llm_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.agent_llm_model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
    }
    try:
        response = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {settings.agent_llm_api_key}"},
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"transport error: {exc}") from exc
    if response.status_code != 200:
        raise LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
    try:
        return response.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError) as exc:
        raise LLMError("malformed chat completion response") from exc
