"""Synthetic dataset evaluation (Phase 12, README "run the synthetic dataset
through the system and calculate final metrics").

Runs the complete deterministic seed (80 customers, 227 orders, 236 payments)
through the real pipeline - event intake, risk evaluation, agent, safety gate,
executor, scripted simulated outcomes, verification and attribution - then
computes the final recovery metrics from stored data only.

Every step is the production code path: failures arrive as synthetic events,
the Phase 11 job layer drives the agent, execution stays the single deliberate
intervention, outcomes are simulated deterministically (seeded RNG per case)
and revenue is credited only by verification. Run from backend/:

    .venv\\Scripts\\python -m evaluation            # resets, reseeds, evaluates, prints metrics
    .venv\\Scripts\\python -m evaluation --no-reset # evaluate the current database state
"""
import argparse
import random
import sys
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from db import SessionLocal
from models import Payment, PaymentStatus, RecoveryCase, RecoveryCaseStatus
from services.action_executor import execute_action
from services.event_intake import build_envelope, process_envelope
from services.jobs import SWEEP_JOB_KEY, run_due_jobs
from services.metrics import compute_metrics
from services.outcome_simulator import simulate_outcome

TRUNCATE_SQL = (
    "TRUNCATE TABLE payment_events, audit_logs, agent_actions, recovery_cases, background_jobs, "
    "payments, orders, customers RESTART IDENTITY CASCADE"
)


def _reset_database(db: Session) -> None:
    db.execute(text(TRUNCATE_SQL))
    db.commit()
    from database.seed import build_customers, build_orders_and_payments

    rng = random.Random(42)
    customers = build_customers(rng)
    orders, payments = build_orders_and_payments(rng, customers)
    db.add_all(customers)
    db.add_all(orders)
    db.add_all(payments)
    db.commit()


def _inject_failure_events(db: Session) -> int:
    failed = db.query(Payment).filter(Payment.status == PaymentStatus.FAILED).order_by(Payment.id).all()
    for payment in failed:
        envelope = build_envelope(
            db,
            {
                "payment_id": payment.payment_id,
                "event": "payment.failed",
                "error_description": payment.failure_reason or "Unknown failure",
            },
        )
        process_envelope(db, envelope, "SYNTHETIC")
    return len(failed)


def _pending_jobs(db: Session) -> int:
    from models import BackgroundJob

    return (
        db.query(BackgroundJob)
        .filter(BackgroundJob.status == "PENDING", BackgroundJob.job_key != SWEEP_JOB_KEY)
        .count()
    )


def _pick_outcome(rng: random.Random, case: RecoveryCase) -> str:
    if case.selected_action == "WAIT_AND_MONITOR":
        return "SUCCESS" if rng.random() < 0.3 else "NO_RESPONSE"
    return "SUCCESS" if rng.random() < 0.6 else "FAILED"


def _drive_loop(db: Session) -> dict:
    rng = random.Random(42)
    scripted: set[tuple[int, int]] = set()
    executed: set[tuple[int, int]] = set()
    passes = 0
    while True:
        passes += 1
        progressed = False

        for case in db.query(RecoveryCase).order_by(RecoveryCase.id).all():
            if case.status == RecoveryCaseStatus.SAFETY_CHECK:
                marker = (case.id, case.attempt_count + 1)
                if marker not in executed:
                    execute_action(db, case)
                    executed.add(marker)
                    progressed = True

        for case in (
            db.query(RecoveryCase)
            .filter(RecoveryCase.status == RecoveryCaseStatus.WAITING_FOR_RESULT)
            .order_by(RecoveryCase.id)
            .all()
        ):
            marker = (case.id, case.attempt_count)
            if marker not in scripted:
                simulate_outcome(db, case, _pick_outcome(rng, case))
                scripted.add(marker)
                progressed = True

        run_due_jobs(db, force=True)
        db.expire_all()
        if not progressed and _pending_jobs(db) == 0:
            break
        if passes > 50:
            raise RuntimeError("evaluation loop did not converge")
    return {"passes": passes, "executions": len(executed), "scripted_outcomes": len(scripted)}


def run_evaluation(reset: bool = True) -> dict:
    db = SessionLocal()
    try:
        if reset:
            print("resetting to the pristine seed (seed=42) ...")
            _reset_database(db)
        db.expire_all()

        print("injecting failure events for every FAILED seed payment ...")
        failed_events = _inject_failure_events(db)
        db.expire_all()
        cases_created = db.query(RecoveryCase).count()
        print(f"  {failed_events} failure events processed -> {cases_created} recovery cases")

        print("running the agent for every scheduled case (Phase 11 jobs) ...")
        run_due_jobs(db, force=True)
        db.expire_all()

        print("driving the loop: execute -> scripted outcome -> verify (deterministic, seed=42) ...")
        stats = _drive_loop(db)
        print(f"  converged after {stats['passes']} passes: {stats['executions']} executions, {stats['scripted_outcomes']} scripted outcomes")

        db.expire_all()
        metrics = compute_metrics(db)
        return metrics
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic dataset evaluation (Phase 12)")
    parser.add_argument("--no-reset", action="store_true", help="keep the current database instead of reseeding")
    args = parser.parse_args()

    import json

    metrics = run_evaluation(reset=not args.no_reset)
    print(json.dumps(metrics, indent=2))
    print("\nfinal metrics computed from stored data only (services.metrics.compute_metrics)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
