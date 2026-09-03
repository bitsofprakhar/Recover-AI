"""Deterministic synthetic dataset generator for RecoverAI.

Generates customers, orders and payments covering successful, failed,
pending, abandoned and ambiguous scenarios. recovery_cases, agent_actions
and audit_logs are intentionally left empty: they are produced by the
recovery pipeline in later phases when events flow through the system.

Run from the backend/ directory:
    python -m database.seed            # seed only if empty
    python -m database.seed --reset    # truncate and reseed
"""
import argparse
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from config import settings
from db import SessionLocal
from models import Customer, Order, Payment
from models.enums import OrderStatus, PaymentStatus

SEED = settings.seed
WINDOW_END = datetime(2026, 8, 30, 18, 0, 0, tzinfo=timezone.utc)
TIMELINE_MINUTES = 60 * 24 * 10

N_CUSTOMERS = 80
N_SUCCESSFUL = 70
N_FAILED_ELIGIBLE = 100
N_PENDING = 25
N_ABANDONED = 20
N_AMBIGUOUS_AMOUNT_MISMATCH = 5
N_AMBIGUOUS_CONFLICTING_STATE = 4
N_AMBIGUOUS_MISSING_ORDER = 3
N_AMBIGUOUS_REPEATED_UNCERTAIN = 3

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Reyansh", "Ishaan", "Dhruv",
    "Ananya", "Diya", "Aadhya", "Kiara", "Myra", "Anika", "Navya", "Sara",
    "Rohan", "Kabir", "Zara", "Meera",
]
LAST_NAMES = [
    "Sharma", "Verma", "Patel", "Reddy", "Nair", "Iyer", "Gupta", "Mehta",
    "Singh", "Kaur", "Chopra", "Bose", "Das", "Rao", "Joshi", "Kulkarni",
    "Malhotra", "Pillai", "Shetty", "Bhatt",
]
METHODS = ["UPI", "CARD", "NETBANKING", "WALLET"]
METHOD_WEIGHTS = [50, 25, 15, 10]
FAILURE_REASONS = [
    "INSUFFICIENT_FUNDS", "AUTHENTICATION_FAILED", "BANK_TIMEOUT", "NETWORK_ERROR",
    "CARD_DECLINED", "RISK_BLOCKED", "INVALID_VPA", "LIMIT_EXCEEDED",
]
FAILURE_WEIGHTS = [22, 18, 14, 12, 10, 8, 8, 8]
UNCERTAIN_REASONS = ["NETWORK_ERROR", "BANK_TIMEOUT"]
AMOUNT_BANDS = [(100, 500, 30), (500, 2500, 40), (2500, 15000, 22), (15000, 30000, 8)]
MISMATCH_FACTORS = [Decimal("0.5"), Decimal("1.5"), Decimal("2")]
UPI_APPS = ["okhdfcbank", "ybl", "paytm", "okaxis", "ibl"]
BANKS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK"]
CARD_NETWORKS = ["VISA", "MASTERCARD", "RUPAY"]
WALLETS = ["PAYTM", "PHONEPE", "AMAZONPAY", "MOBIKWIK"]


def rand_amount(rng: random.Random) -> Decimal:
    band = rng.choices([b[:2] for b in AMOUNT_BANDS], weights=[b[2] for b in AMOUNT_BANDS])[0]
    return Decimal(str(round(rng.uniform(band[0], band[1]))))


def rand_method(rng: random.Random) -> str:
    return rng.choices(METHODS, weights=METHOD_WEIGHTS)[0]


def rand_failure_reason(rng: random.Random) -> str:
    return rng.choices(FAILURE_REASONS, weights=FAILURE_WEIGHTS)[0]


def rand_timestamps(rng: random.Random) -> tuple[datetime, datetime]:
    order_ts = WINDOW_END - timedelta(minutes=rng.uniform(0, TIMELINE_MINUTES))
    payment_ts = order_ts + timedelta(minutes=rng.uniform(1, 30))
    return order_ts, payment_ts


def build_metadata(rng: random.Random, method: str) -> dict:
    metadata = {"gateway": "razorpay", "mode": "test"}
    if method == "UPI":
        metadata["vpa"] = f"user{rng.randint(1000, 9999)}@{rng.choice(UPI_APPS)}"
    elif method == "CARD":
        metadata["last4"] = str(rng.randint(1000, 9999))
        metadata["network"] = rng.choice(CARD_NETWORKS)
    elif method == "NETBANKING":
        metadata["bank"] = rng.choice(BANKS)
    else:
        metadata["wallet"] = rng.choice(WALLETS)
    return metadata


def build_customers(rng: random.Random) -> list[Customer]:
    customers = []
    for i in range(1, N_CUSTOMERS + 1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        roll = rng.random()
        if roll < 0.60:
            lifetime = rng.randint(20, 60)
            successes = round(lifetime * rng.uniform(0.85, 0.98))
            prior_attempts = 0
            prior_successes = 0
        elif roll < 0.85:
            lifetime = rng.randint(10, 25)
            successes = round(lifetime * rng.uniform(0.70, 0.85))
            prior_attempts = 0
            prior_successes = 0
        else:
            lifetime = rng.randint(3, 12)
            successes = round(lifetime * rng.uniform(0.30, 0.60))
            prior_attempts = rng.randint(1, 4)
            prior_successes = rng.randint(0, prior_attempts)
        customers.append(
            Customer(
                customer_id=f"cust_{i:04d}",
                name=f"{first} {last}",
                email=f"{first.lower()}.{last.lower()}{i:02d}@example.com",
                phone="+91" + "".join(rng.choice("0123456789") for _ in range(10)),
                lifetime_payments=lifetime,
                lifetime_successes=successes,
                prior_recovery_attempts=prior_attempts,
                prior_recovery_successes=prior_successes,
                created_at=WINDOW_END - timedelta(days=rng.uniform(180, 400)),
            )
        )
    return customers


def build_orders_and_payments(rng: random.Random, customers: list[Customer]):
    orders: list[Order] = []
    payments: list[Payment] = []
    counters = {"order": 0, "payment": 0}

    def new_order(customer: Customer, amount: Decimal, status: OrderStatus, created_at: datetime) -> Order:
        counters["order"] += 1
        order = Order(
            order_id=f"order_{counters['order']:04d}",
            customer=customer,
            amount=amount,
            currency="INR",
            status=status,
            created_at=created_at,
        )
        orders.append(order)
        return order

    def new_payment(
        order: Order | None,
        amount: Decimal,
        method: str,
        status: PaymentStatus,
        created_at: datetime,
        failure_reason: str | None = None,
        metadata: dict | None = None,
    ) -> Payment:
        counters["payment"] += 1
        payment = Payment(
            payment_id=f"pay_{counters['payment']:04d}",
            order=order,
            amount=amount,
            method=method,
            status=status,
            failure_reason=failure_reason,
            gateway_metadata=metadata,
            created_at=created_at,
            updated_at=created_at,
        )
        payments.append(payment)
        return payment

    for _ in range(N_SUCCESSFUL):
        customer = rng.choice(customers)
        amount = rand_amount(rng)
        method = rand_method(rng)
        order_ts, payment_ts = rand_timestamps(rng)
        order = new_order(customer, amount, OrderStatus.PAID, order_ts)
        new_payment(order, amount, method, PaymentStatus.CAPTURED, payment_ts, metadata=build_metadata(rng, method))

    for _ in range(N_FAILED_ELIGIBLE):
        customer = rng.choice(customers)
        amount = rand_amount(rng)
        method = rand_method(rng)
        order_ts, payment_ts = rand_timestamps(rng)
        order = new_order(customer, amount, OrderStatus.ATTEMPTED, order_ts)
        new_payment(
            order,
            amount,
            method,
            PaymentStatus.FAILED,
            payment_ts,
            failure_reason=rand_failure_reason(rng),
            metadata=build_metadata(rng, method),
        )

    for _ in range(N_PENDING):
        customer = rng.choice(customers)
        amount = rand_amount(rng)
        method = rand_method(rng)
        order_ts, payment_ts = rand_timestamps(rng)
        order = new_order(customer, amount, OrderStatus.CREATED, order_ts)
        new_payment(order, amount, method, PaymentStatus.PENDING, payment_ts, metadata=build_metadata(rng, method))

    for _ in range(N_ABANDONED):
        customer = rng.choice(customers)
        amount = rand_amount(rng)
        method = rand_method(rng)
        order_ts, payment_ts = rand_timestamps(rng)
        order = new_order(customer, amount, OrderStatus.CREATED, order_ts)
        new_payment(order, amount, method, PaymentStatus.CREATED, payment_ts, metadata=build_metadata(rng, method))

    for _ in range(N_AMBIGUOUS_AMOUNT_MISMATCH):
        customer = rng.choice(customers)
        amount = rand_amount(rng)
        method = rand_method(rng)
        order_ts, payment_ts = rand_timestamps(rng)
        order = new_order(customer, amount, OrderStatus.ATTEMPTED, order_ts)
        mismatched = (amount * rng.choice(MISMATCH_FACTORS)).quantize(Decimal("1"))
        new_payment(
            order,
            mismatched,
            method,
            PaymentStatus.FAILED,
            payment_ts,
            failure_reason=rand_failure_reason(rng),
            metadata=build_metadata(rng, method),
        )

    for _ in range(N_AMBIGUOUS_CONFLICTING_STATE):
        customer = rng.choice(customers)
        amount = rand_amount(rng)
        method = rand_method(rng)
        order_ts, payment_ts = rand_timestamps(rng)
        order = new_order(customer, amount, OrderStatus.PAID, order_ts)
        new_payment(
            order,
            amount,
            method,
            PaymentStatus.FAILED,
            payment_ts,
            failure_reason=rand_failure_reason(rng),
            metadata=build_metadata(rng, method),
        )

    for _ in range(N_AMBIGUOUS_MISSING_ORDER):
        amount = rand_amount(rng)
        method = rand_method(rng)
        _, payment_ts = rand_timestamps(rng)
        new_payment(
            None,
            amount,
            method,
            PaymentStatus.FAILED,
            payment_ts,
            failure_reason=rand_failure_reason(rng),
            metadata=build_metadata(rng, method),
        )

    for _ in range(N_AMBIGUOUS_REPEATED_UNCERTAIN):
        customer = rng.choice(customers)
        amount = rand_amount(rng)
        method = rand_method(rng)
        order_ts, payment_ts = rand_timestamps(rng)
        order = new_order(customer, amount, OrderStatus.ATTEMPTED, order_ts)
        metadata = build_metadata(rng, method)
        for k in range(3):
            new_payment(
                order,
                amount,
                method,
                PaymentStatus.FAILED,
                payment_ts + timedelta(minutes=3 * (k + 1)),
                failure_reason=rng.choice(UNCERTAIN_REASONS),
                metadata=metadata,
            )

    return orders, payments


def print_summary(db) -> None:
    def scalar(stmt: str):
        return db.execute(text(stmt)).scalar()

    print(f"seed={SEED} window_end={WINDOW_END.isoformat()}")
    print(f"customers={scalar('SELECT count(*) FROM customers')}")
    print(f"orders={scalar('SELECT count(*) FROM orders')}")
    print(f"payment_events={scalar('SELECT count(*) FROM payment_events')}")
    for status, count in db.execute(text("SELECT status, count(*) FROM payments GROUP BY status ORDER BY status")).all():
        print(f"payments.{status.lower()}={count}")
    print(
        "ambiguous: amount_mismatch="
        + str(scalar("SELECT count(*) FROM payments p JOIN orders o ON p.order_id = o.id WHERE p.status = 'FAILED' AND p.amount <> o.amount"))
        + " conflicting_state="
        + str(scalar("SELECT count(*) FROM payments p JOIN orders o ON p.order_id = o.id WHERE p.status = 'FAILED' AND o.status = 'PAID'"))
        + " missing_order="
        + str(scalar("SELECT count(*) FROM payments WHERE order_id IS NULL"))
    )
    at_risk = scalar(
        "SELECT coalesce(sum(o.amount), 0) FROM payments p JOIN orders o ON p.order_id = o.id "
        "WHERE p.status = 'FAILED' AND p.amount = o.amount"
    )
    print(f"order amounts on cleanly failed payments (revenue-at-risk preview): {at_risk}")
    print("recovery_cases/agent_actions/audit_logs are empty by design (filled by the pipeline in Phases 3-9)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the RecoverAI synthetic dataset")
    parser.add_argument("--reset", action="store_true", help="truncate all tables before seeding")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        try:
            existing = db.scalar(text("SELECT count(*) FROM payments"))
        except (ProgrammingError, OperationalError) as exc:
            print(f"database not ready ({exc.__class__.__name__}); run: alembic upgrade head")
            return
        if existing and not args.reset:
            print(f"payments already contains {existing} rows; nothing to do (use --reset to truncate and reseed)")
            return
        if args.reset:
            db.execute(
                text(
                    "TRUNCATE TABLE payment_events, audit_logs, agent_actions, recovery_cases, background_jobs, "
                    "payments, orders, customers RESTART IDENTITY CASCADE"
                )
            )
            db.commit()

        rng = random.Random(SEED)
        customers = build_customers(rng)
        orders, payments = build_orders_and_payments(rng, customers)

        db.add_all(customers)
        db.add_all(orders)
        db.add_all(payments)
        db.commit()
        print_summary(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
