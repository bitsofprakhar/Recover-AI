"""Phase 11 tests: background job scheduling, execution, the expiry sweep and the APIs."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from models import (
    AuditLog,
    BackgroundJob,
    OrderStatus,
    Payment,
    PaymentStatus,
    RecoveryCase,
    RecoveryCaseStatus,
)
from services.action_executor import execute_action
from services.agent import run_agent
from services.jobs import (
    SWEEP_JOB_KEY,
    ensure_recurring_jobs,
    list_jobs,
    run_due_jobs,
    schedule_job,
    sweep_expired_cases,
)
from services.outcome_simulator import simulate_outcome


def _make_case(db, make_customer, make_order, make_payment, post_event, amount=Decimal("2000.00"), **customer_kwargs):
    customer = make_customer(**customer_kwargs)
    order = make_order(customer=customer, amount=amount)
    payment = make_payment(order=order, amount=amount, status=PaymentStatus.PENDING)
    result = post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    assert result["risk_evaluation"]["decision"] == "CASE_CREATED"
    case = db.query(RecoveryCase).filter(RecoveryCase.payment_id == payment.id).one()
    db.expire_all()
    return case


def _refresh(db, case):
    db.expire_all()
    return db.query(RecoveryCase).filter(RecoveryCase.id == case.id).one()


def _job(db, key):
    return db.query(BackgroundJob).filter(BackgroundJob.job_key == key).one_or_none()


def _audits(db, case):
    return db.query(AuditLog).filter(AuditLog.case_id == case.id).order_by(AuditLog.id).all()


def test_case_creation_schedules_agent_run_automatically(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    job = _job(db, f"agent:{case.id}:0")

    assert job is not None
    assert job.name == "run_agent"
    assert job.params == {"case_id": case.id}
    assert job.status == "PENDING"
    escalated = db.query(RecoveryCase).filter(RecoveryCase.status == RecoveryCaseStatus.ESCALATED).count()
    if escalated == 0:
        assert job.due_at <= datetime.now(timezone.utc) + timedelta(seconds=1)


def test_born_escalated_cases_never_schedule_an_agent(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("1500.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    result = post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    assert result["risk_evaluation"]["decision"] == "CASE_ESCALATED"

    assert db.query(BackgroundJob).count() == 0


def test_run_due_jobs_executes_scheduled_agent_run(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    result = run_due_jobs(db)

    assert result["executed_count"] == 1
    entry = result["executed"][0]
    assert entry["job_key"] == f"agent:{case.id}:0"
    assert entry["status"] == "DONE"
    assert entry["result"]["decision"] == "SAFETY_CHECK"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    assert case.score is not None
    assert case.selected_action == "RETRY_PAYMENT_LINK"
    events = [log.event_type for log in _audits(db, case)]
    assert "agent.diagnosis_completed" in events
    assert "gate.allowed" in events


def test_execution_schedules_delayed_verification_and_job_runs_it(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    run_due_jobs(db)
    case = _refresh(db, case)
    execute_action(db, case)

    job = _job(db, f"verify:{case.id}:1:executed")

    assert job is not None
    assert job.name == "verify_outcome"
    assert job.status == "PENDING"
    assert job.due_at > datetime.now(timezone.utc)

    not_due = run_due_jobs(db)
    assert not_due["executed_count"] == 0
    assert _job(db, f"verify:{case.id}:1:executed").status == "PENDING"

    result = run_due_jobs(db, force=True)

    verify_entries = [item for item in result["executed"] if item["name"] == "verify_outcome"]
    assert len(verify_entries) == 1
    assert verify_entries[0]["result"]["result"] == "NO_SUCCESS_YET"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.WAITING_FOR_RESULT
    assert case.recovered_payment_id is None

    again = run_due_jobs(db, force=True)
    assert again["executed_count"] == 0
    assert _job(db, f"verify:{case.id}:1:executed").status == "DONE"


def test_success_outcome_schedules_verification_and_case_recovers_automatically(
    db, make_customer, make_order, make_payment, post_event
):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    run_due_jobs(db)
    execute_action(db, _refresh(db, case))

    simulate_outcome(db, _refresh(db, case), "SUCCESS")

    job = _job(db, f"verify:{case.id}:1:outcome")
    assert job is not None
    assert job.status == "PENDING"
    assert job.due_at <= datetime.now(timezone.utc) + timedelta(seconds=1)

    result = run_due_jobs(db)

    verify_entries = [item for item in result["executed"] if item["job_key"] == f"verify:{case.id}:1:outcome"]
    assert verify_entries[0]["result"]["decision"] == "RECOVERED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.RECOVERED
    assert case.recovered_payment_id == f"pay_rec_{case.id}_1"
    assert case.recovered_amount == Decimal("2000.00")


def test_failed_outcome_schedules_next_agent_run_automatically(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    run_due_jobs(db)
    execute_action(db, _refresh(db, case))

    simulate_outcome(db, _refresh(db, case), "FAILED")

    job = _job(db, f"agent:{case.id}:1")
    assert job is not None
    assert job.status == "PENDING"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.DIAGNOSING

    result = run_due_jobs(db)

    agent_entries = [item for item in result["executed"] if item["job_key"] == f"agent:{case.id}:1"]
    assert agent_entries[0]["status"] == "DONE"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    assert case.attempt_count == 1


def test_full_autonomous_retry_cycle_via_jobs(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    run_due_jobs(db)
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    execute_action(db, case)

    simulate_outcome(db, _refresh(db, case), "FAILED")
    run_due_jobs(db)
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK
    execute_action(db, case)
    assert _refresh(db, case).attempt_count == 2

    simulate_outcome(db, _refresh(db, case), "FAILED")
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.NOT_RECOVERED
    assert _job(db, f"agent:{case.id}:2") is None


def test_sweep_stops_expired_waiting_case(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    run_due_jobs(db)
    execute_action(db, _refresh(db, case))
    case = _refresh(db, case)
    case.expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    result = sweep_expired_cases(db)

    assert result["expired_cases"] == 1
    assert result["stopped"] == [{"case_id": case.id, "from_status": "WAITING_FOR_RESULT"}]
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.STOPPED
    log = _audits(db, case)[-1]
    assert log.event_type == "case.window_expired"
    assert log.to_status == "STOPPED"
    assert log.payload["detected_by"] == "expiry_sweep"

    again = sweep_expired_cases(db)
    assert again["expired_cases"] == 0
    assert again["stopped"] == []


def test_sweep_stops_expired_case_from_any_active_state(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.DETECTED
    case.expiry = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()

    result = sweep_expired_cases(db)

    assert result["stopped"] == [{"case_id": case.id, "from_status": "DETECTED"}]
    assert _refresh(db, case).status == RecoveryCaseStatus.STOPPED


def test_sweep_never_touches_terminal_cases(db, make_customer, make_order, make_payment, post_event):
    customer = make_customer()
    order = make_order(customer=customer, amount=Decimal("2000.00"))
    payment = make_payment(order=order, amount=Decimal("2000.00"), status=PaymentStatus.PENDING)
    post_event(
        {"payment_id": payment.payment_id, "event": "payment.failed", "error_description": "Insufficient funds"}
    )
    case = db.query(RecoveryCase).one()
    run_agent(db, case.id)
    case = _refresh(db, case)
    assert case.selected_action == "RETRY_PAYMENT_LINK"
    execute_action(db, case)
    simulate_outcome(db, _refresh(db, case), "FAILED")
    run_due_jobs(db)
    execute_action(db, _refresh(db, case))
    simulate_outcome(db, _refresh(db, case), "FAILED")
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.NOT_RECOVERED

    result = sweep_expired_cases(db)

    assert result["expired_cases"] == 0
    assert _refresh(db, case).status == RecoveryCaseStatus.NOT_RECOVERED


def test_recurring_sweep_job_reschedules_itself(db, make_customer, make_order, make_payment, post_event):
    seeded = ensure_recurring_jobs(db)
    db.commit()

    assert seeded["created"] is True
    assert seeded["job_key"] == SWEEP_JOB_KEY
    assert seeded["recurring_interval_seconds"] is not None

    again = ensure_recurring_jobs(db)
    db.commit()
    assert again["created"] is False

    result = run_due_jobs(db, force=True)

    sweep_entries = [item for item in result["executed"] if item["job_key"] == SWEEP_JOB_KEY]
    assert len(sweep_entries) == 1
    job = _job(db, SWEEP_JOB_KEY)
    assert job.status == "PENDING"
    assert job.due_at > datetime.now(timezone.utc)
    assert job.result["expired_cases"] == 0
    assert db.query(BackgroundJob).filter(BackgroundJob.job_key == SWEEP_JOB_KEY).count() == 1


def test_schedule_job_is_idempotent_by_key(db):
    first = schedule_job(db, "verify_outcome", {"case_id": 1}, datetime.now(timezone.utc), "verify:1:1:executed")
    second = schedule_job(db, "verify_outcome", {"case_id": 1}, datetime.now(timezone.utc), "verify:1:1:executed")
    db.commit()

    assert first["created"] is True
    assert second["created"] is False
    assert db.query(BackgroundJob).filter(BackgroundJob.job_key == "verify:1:1:executed").count() == 1


def test_unknown_job_name_fails_without_blocking_other_jobs(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    schedule_job(db, "bogus_job", {}, datetime.now(timezone.utc), "bogus:1")
    db.commit()

    result = run_due_jobs(db)

    by_key = {item["job_key"]: item for item in result["executed"]}
    assert by_key[f"agent:{case.id}:0"]["status"] == "DONE"
    assert by_key["bogus:1"]["status"] == "FAILED"
    assert "UNKNOWN_JOB" in by_key["bogus:1"]["error"]
    assert db.query(BackgroundJob).filter(BackgroundJob.job_key == "bogus:1").one().status == "FAILED"
    case = _refresh(db, case)
    assert case.status == RecoveryCaseStatus.SAFETY_CHECK


def test_run_due_jobs_skips_jobs_not_yet_due(db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    schedule_job(
        db,
        "run_agent",
        {"case_id": case.id},
        datetime.now(timezone.utc) + timedelta(hours=1),
        f"agent:{case.id}:future",
    )
    db.commit()

    result = run_due_jobs(db)

    assert result["executed_count"] == 1  # the creation-scheduled agent job only
    assert _job(db, f"agent:{case.id}:future").status == "PENDING"


def test_scheduler_tick_ensures_recurring_and_runs_due(db, make_customer, make_order, make_payment, post_event):
    from services.scheduler import scheduler_tick

    _make_case(db, make_customer, make_order, make_payment, post_event)

    result = scheduler_tick(db)

    assert _job(db, SWEEP_JOB_KEY) is not None
    keys = [item["job_key"] for item in result["executed"]]
    assert f"agent:1:0" in keys


def test_jobs_and_run_api_endpoints(client, db, make_customer, make_order, make_payment, post_event):
    response = client.get("/api/jobs")
    assert response.status_code == 200
    assert response.json() == {"total": 0, "items": []}

    response = client.get("/api/jobs?status=BOGUS")
    assert response.status_code == 422

    case = _make_case(db, make_customer, make_order, make_payment, post_event)

    response = client.get("/api/jobs")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["job_key"] == f"agent:{case.id}:0"

    response = client.post("/api/jobs/run", json={"force": True})
    assert response.status_code == 200
    body = response.json()
    assert body["forced"] is True
    assert body["executed_count"] == 1
    assert body["executed"][0]["status"] == "DONE"

    detail = client.get(f"/api/cases/{case.id}").json()
    assert detail["status"] == "SAFETY_CHECK"


def test_scheduled_outcome_endpoint_and_execution(client, db, make_customer, make_order, make_payment, post_event):
    case = _make_case(db, make_customer, make_order, make_payment, post_event)
    run_due_jobs(db)
    execute_action(db, _refresh(db, case))

    response = client.post(f"/api/cases/{case.id}/outcome", json={"outcome": "SUCCESS", "delay_seconds": 60})
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "SCHEDULED"
    assert body["scheduled_outcome"]["created"] is True
    key = body["scheduled_outcome"]["job_key"]
    assert key == f"outcome:{case.id}:1:SUCCESS"

    response = client.post(f"/api/cases/{case.id}/outcome", json={"outcome": "SUCCESS", "delay_seconds": 60})
    assert response.json()["scheduled_outcome"]["created"] is False

    not_due = run_due_jobs(db)
    assert key not in [item["job_key"] for item in not_due["executed"]]

    forced = run_due_jobs(db, force=True)
    executed = {item["job_key"]: item for item in forced["executed"]}
    assert executed[key]["status"] == "DONE"
    assert executed[key]["result"]["simulated"] is True
    case = _refresh(db, case)
    recovery = db.query(Payment).filter(Payment.payment_id == f"pay_rec_{case.id}_1").one()
    assert recovery.status == PaymentStatus.CAPTURED

    verify_forced = run_due_jobs(db, force=True)
    executed2 = {item["job_key"]: item for item in verify_forced["executed"]}
    assert executed2[f"verify:{case.id}:1:outcome"]["result"]["decision"] == "RECOVERED"
    assert _refresh(db, case).status == RecoveryCaseStatus.RECOVERED
