"""Test configuration: dedicated recoverai_test database with the real migrated schema.

The DATABASE_URL environment variable is set before any application module is
imported so every engine in the test process points at the test database. The
session fixture resets the schema with the real Alembic migrations; each test
starts from truncated tables.
"""
import os
from pathlib import Path

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://recoverai:recoverai@localhost:5432/recoverai_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Phase 11: tests exercise the job layer deterministically via run_due_jobs /
# POST /api/jobs/run; the in-process scheduler stays off in the test process.
os.environ["SCHEDULER_ENABLED"] = "false"

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from db import SessionLocal
from models import Customer, Order, OrderStatus, Payment, PaymentStatus

BACKEND_DIR = Path(__file__).resolve().parent.parent

TRUNCATE_SQL = (
    "TRUNCATE TABLE audit_logs, agent_actions, recovery_cases, background_jobs, payment_events, "
    "payments, orders, customers RESTART IDENTITY CASCADE"
)


@pytest.fixture(scope="session", autouse=True)
def schema():
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "database" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    engine.dispose()
    yield


@pytest.fixture()
def db():
    session = SessionLocal()
    session.execute(text(TRUNCATE_SQL))
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def make_customer(db):
    counter = iter(range(1, 1000))

    def _make(**kwargs):
        n = next(counter)
        defaults = dict(
            customer_id=f"cust_{n:04d}",
            name=f"Customer {n}",
            email=f"customer{n}@example.com",
            phone="+919999999999",
            lifetime_payments=10,
            lifetime_successes=9,
            prior_recovery_attempts=0,
            prior_recovery_successes=0,
        )
        defaults.update(kwargs)
        customer = Customer(**defaults)
        db.add(customer)
        db.flush()
        return customer

    return _make


@pytest.fixture()
def make_order(db):
    counter = iter(range(1, 1000))

    def _make(customer, amount, status=OrderStatus.CREATED, **kwargs):
        n = next(counter)
        defaults = dict(
            order_id=f"order_{n:04d}",
            customer_id=customer.id,
            amount=amount,
            currency="INR",
            status=status,
        )
        defaults.update(kwargs)
        order = Order(**defaults)
        db.add(order)
        db.flush()
        return order

    return _make


@pytest.fixture()
def make_payment(db):
    counter = iter(range(1, 1000))

    def _make(order, amount, status=PaymentStatus.PENDING, failure_reason=None, **kwargs):
        n = next(counter)
        defaults = dict(
            payment_id=f"pay_{n:04d}",
            order_id=order.id if order is not None else None,
            amount=amount,
            method="UPI",
            status=status,
            failure_reason=failure_reason,
            gateway_metadata={"gateway": "razorpay", "mode": "test"},
        )
        defaults.update(kwargs)
        payment = Payment(**defaults)
        db.add(payment)
        db.flush()
        return payment

    return _make


@pytest.fixture()
def post_event(db):
    """Post an event spec through the full pipeline (envelope built, processed, committed)."""
    from services.event_intake import build_envelope, process_envelope

    counter = iter(range(1, 10000))

    def _post(spec: dict, source: str = "SYNTHETIC") -> dict:
        spec = dict(spec)
        spec.setdefault("created_at", 1756600000 + next(counter))
        envelope = build_envelope(db, spec)
        return process_envelope(db, envelope, source)

    return _post


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from db import get_db
    from main import app

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
