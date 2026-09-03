"""Recovery metrics (Phase 9, README Section 11).

Every number is computed from stored data only - cases, payments, agent
actions and audit rows - so the dashboard (Phase 10) can never display
anything the pipeline did not produce. Recovered revenue counts only
verified successful amounts attributable to completed (RECOVERED) recovery
cases; the recovery rate divides it by the eligible revenue at risk.
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from models import TERMINAL_CASE_STATUSES, AgentAction, RecoveryCase, RecoveryCaseStatus


def compute_metrics(db: Session) -> dict:
    cases = db.query(RecoveryCase).order_by(RecoveryCase.id).all()
    total = len(cases)
    by_status = Counter(case.status.value for case in cases)
    active = sum(1 for case in cases if case.status not in TERMINAL_CASE_STATUSES)
    escalated = by_status.get(RecoveryCaseStatus.ESCALATED.value, 0)

    total_risk = sum((case.revenue_at_risk for case in cases), Decimal("0"))
    escalated_risk = sum(
        (case.revenue_at_risk for case in cases if case.status == RecoveryCaseStatus.ESCALATED),
        Decimal("0"),
    )
    eligible_risk = total_risk - escalated_risk

    recovered_cases = [case for case in cases if case.status == RecoveryCaseStatus.RECOVERED]
    recovered_revenue = sum((case.recovered_amount or Decimal("0") for case in recovered_cases), Decimal("0"))
    recovery_rate = float(recovered_revenue / eligible_risk) if eligible_risk > 0 else None

    attempts = sum(case.attempt_count for case in cases)

    durations = [
        (case.recovered_at - case.created_at).total_seconds()
        for case in recovered_cases
        if case.recovered_at is not None
    ]
    average_recovery_seconds = (sum(durations) / len(durations)) if durations else None

    blocked_rows = db.query(AgentAction).filter(AgentAction.allowed.is_(False)).all()
    blocked_by_tool = Counter(row.tool_name for row in blocked_rows)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": {
            "total": total,
            "active": active,
            "by_status": dict(sorted(by_status.items())),
        },
        "revenue_at_risk": {
            "total": str(total_risk),
            "eligible": str(eligible_risk),
            "escalated_excluded": str(escalated_risk),
            "note": (
                "eligible excludes ESCALATED cases: their ambiguity went to human review, so the agent "
                "had no legitimate recovery opportunity on them; they are measured by the escalation rate"
            ),
        },
        "recovered": {
            "revenue": str(recovered_revenue),
            "cases": len(recovered_cases),
        },
        "recovery_rate": recovery_rate,
        "attempts": {
            "total": attempts,
            "successful_recoveries": len(recovered_cases),
        },
        "average_recovery_time": (
            {"seconds": average_recovery_seconds, "cases_counted": len(durations)} if durations else None
        ),
        "escalation_rate": (escalated / total) if total else None,
        "invalid_or_blocked_actions": {
            "total": len(blocked_rows),
            "by_tool": dict(sorted(blocked_by_tool.items())),
        },
    }
