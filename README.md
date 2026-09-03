# RecoverAI

**Autonomous Revenue Recovery Agent**
Razorpay Buildathon · Track 3: AI Revenue Recovery

> **North Star:** Detect → Diagnose → Decide → Act → Verify → Recover.
>
> RecoverAI is an AI agent that identifies revenue at risk, understands why it is at risk, chooses the right bounded recovery action, executes it safely, verifies the outcome and measures recovered revenue.

**Final product flow:** Detect → Diagnose → Score → Decide → Safety Check → Act → Verify → Attribute → Recover → Audit.

**Core principle:** build one complete, measurable, safe loop before adding more workflows.

---

## Contents

1. [Project Overview](#1-project-overview)
2. [Locked MVP Decisions](#2-locked-mvp-decisions)
3. [System Architecture](#3-system-architecture)
4. [Tech Stack](#4-tech-stack)
5. [Repository Structure](#5-repository-structure)
6. [Data Model](#6-data-model)
7. [Agent Tools](#7-agent-tools)
8. [Recovery Case State Machine](#8-recovery-case-state-machine)
9. [State Transition Rules](#9-state-transition-rules)
10. [Pending-Payment Pre-Flow](#10-pending-payment-pre-flow)
11. [Attribution Rule and Metrics](#11-attribution-rule-and-metrics)
12. [Development Phases](#12-development-phases)
13. [Definition of Done](#13-definition-of-done)
14. [Build Status](#14-build-status)

---

## 1. Project Overview

### The problem

Merchants lose expected revenue when payments fail, remain pending, or customers abandon checkout. The problem is not simply detecting a failed payment — it is closing the loop from revenue-at-risk detection to diagnosis, intervention, execution and measured recovery.

### What RecoverAI is

RecoverAI is an AI agent for merchants. It is **not only a chatbot** and **not only a payment-link generator**. The AI reasons over structured payment/order/customer context and calls controlled backend tools.

**The golden rule:** the LLM selects tools and recommends actions, but the backend decides whether a tool call is actually allowed. The model never bypasses backend controls.

**Example:** a ₹2,000 UPI payment fails. The agent checks the transaction state, failure reason, customer history and existing recovery attempts. If recovery is allowed, it selects an approved recovery action, executes it through a tool, verifies the result, and records ₹2,000 as recovered if the payment succeeds.

### MVP scope

The MVP delivers exactly one complete, reliable loop:

```
Failed payment → diagnosis → safe recovery → verification → recovered revenue → audit log
```

Pending-payment verification and checkout abandonment are extensions. Subscription failure recovery and overdue receivables are out of scope. A reliable narrow agent is better than five incomplete workflows.

---

## 2. Locked MVP Decisions

These decisions are final for the MVP and are followed strictly throughout the build.

| Area | Decision for MVP |
|---|---|
| Primary workflow | Failed payment recovery end-to-end |
| Merchant model | Single-merchant demo mode; no authentication in MVP |
| LLM | GLM 5.3 through the configured agent/router, using structured tool calling |
| Recovery action | Create a simulated recovery payment/link and simulated notification |
| Outcome simulation | Scripted outcome simulator / webhook replay; no fake real payment success |
| Recovered attribution | Count only successful payments attributable to a recovery case/action within its allowed time window |
| Queue | Start with FastAPI background tasks or lightweight scheduler; add Redis + Celery only when needed |
| Notification | Simulated notification service with channel field; no real SMS/WhatsApp dependency |
| Safety defaults | Maximum 2 active recovery attempts per case; 24-hour recovery window |
| Ambiguous case | Conflicting state, missing identity/amount, repeated uncertain failures, duplicate event, or policy conflict |

**Explicitly out of MVP scope:** subscription failures, overdue receivables, checkout abandonment (extension only), real SMS/WhatsApp/email delivery, Razorpay live mode, multi-merchant authentication.

---

## 3. System Architecture

```
┌─────────────────────────────┐
│  React / Next.js            │  Merchant Dashboard
│  cases · AI timeline ·      │
│  metrics                    │
└──────────────┬──────────────┘
               │ REST APIs
┌──────────────▼──────────────┐          ┌───────────────────────────┐
│  FastAPI Backend            │◀─────────│  Razorpay Test Mode       │
│  APIs · Webhooks ·          │ webhooks │  + synthetic events       │
│  Orchestration              │          └───────────────────────────┘
└──────────────┬──────────────┘
               │
    ┌──────────▼───────────┐
    │ Revenue-at-Risk      │  eligible events become
    │ Detection            │  recovery cases
    └──────────┬───────────┘
               │
    ┌──────────▼───────────┐
    │ AI Agent (GLM 5.3)   │  diagnose · score · decide
    │ + Tool Calling       │
    └──────────┬───────────┘
               │ proposed tool call
    ┌──────────▼───────────┐
    │ Safety / Policy      │  idempotency · limits · validation ·
    │ Engine               │  escalation (ALLOW / BLOCK / ESCALATE)
    └──────────┬───────────┘
               │ allowed tool call
    ┌──────────▼───────────────────────────────┐
    │ Recovery Action (simulated)              │
    │ → Outcome Simulator → Verification       │
    │ → Attribution → Audit                    │
    └──────────┬───────────────────────────────┘
               │
┌──────────────▼──────────────┐
│  PostgreSQL                 │  data · state transitions · audit · metrics
└─────────────────────────────┘
```

### Responsibilities

| Component | Responsibility |
|---|---|
| Frontend | Merchant dashboard, recovery cases, AI timeline and metrics |
| FastAPI | APIs, webhooks, orchestration and server-side validation |
| PostgreSQL | Orders, payments, customers, cases, actions and audit logs |
| Razorpay Test Mode + synthetic events | Payment events and permitted payment APIs |
| AI Agent (GLM 5.3) | Diagnosis, reasoning and action selection |
| Safety Engine | Duplicate checks, amount validation, retry limits and escalation |
| Outcome Simulator | Scripted SUCCESS / FAILED / STILL_PENDING / NO_RESPONSE outcomes, explicitly labelled as simulated |

### End-to-end flow

1. A payment event arrives (webhook or synthetic) and is normalized.
2. Revenue-at-risk detection decides whether the event opens a recovery case.
3. The AI agent diagnoses the case: transaction state, order details, customer history, prior attempts.
4. A deterministic recovery score is computed and logged.
5. The agent recommends a recovery action.
6. The safety/policy gate validates it (ALLOW / BLOCK / ESCALATE).
7. The approved action executes: recovery record, simulated payment/link, simulated notification.
8. The outcome simulator / webhook replay produces the payment outcome.
9. Verification confirms the outcome and applies the attribution rule.
10. The terminal state, metrics and audit trail update.

---

## 4. Tech Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js / React + Tailwind CSS |
| Backend | Python + FastAPI |
| Database | PostgreSQL |
| AI | GLM 5.3 via the configured agent/router, structured tool calling |
| Payments | Razorpay Test Mode + locally generated synthetic events |
| Async jobs | FastAPI background tasks / lightweight scheduler (Redis + Celery only when needed) |
| Deployment | Vercel + Render/Railway/AWS |
| Version control | Git + GitHub |

### Integration principles

- Use Test Mode during development; support locally generated synthetic events so the demo never depends on external payment timing.
- Receive payment events through webhook endpoints; fetch or verify current state when immediate verification is needed.
- Keep secrets server-side.
- Simulated recovery actions and notifications are clearly labelled as simulated; no fake real payment success is ever recorded.

---

## 5. Repository Structure

Actual layout, materialized as the phases build it (backend tests live in `backend/tests/`; frontend unit tests in `frontend/src/lib/format.test.ts`):

```
recoverai/
├── README.md
├── .env.example               # environment template (copy to backend/.env)
├── .gitignore
├── docker-compose.yml         # optional containerized PostgreSQL for local dev
├── backend/                   # FastAPI: APIs + webhooks
│   ├── main.py                # FastAPI app (routers + scheduler lifespan + health)
│   ├── config.py              # settings: DB URL, safety constants, webhook secret, seed, scheduler
│   ├── evaluation.py          # Phase 12: synthetic-dataset evaluation harness (python -m evaluation)
│   ├── db.py                  # SQLAlchemy engine + session factory
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── pytest.ini
│   ├── api/                   # REST routes + webhook endpoints
│   │   ├── webhooks.py        # POST /webhooks/razorpay (HMAC signature verification)
│   │   ├── events.py          # synthetic + replay endpoints, event list/detail
│   │   ├── cases.py           # case list/detail, agent run, gate, execute, outcome (optionally scheduled), verify
│   │   ├── demo.py            # POST /api/demo/case (deterministic executable demo case)
│   │   ├── jobs.py            # GET /api/jobs, POST /api/jobs/run (Phase 11 background jobs)
│   │   └── metrics.py         # GET /api/metrics (Section 11 recovery metrics)
│   ├── services/              # domain services
│   │   ├── audit.py           # shared audit-trail writer
│   │   ├── event_intake.py    # normalization, idempotency, payment upsert, audit
│   │   ├── revenue_risk.py    # Phase 4: revenue-at-risk rules + case creation
│   │   ├── case_lifecycle.py  # Phase 5: legal-transition table + audited transitions
│   │   ├── scoring.py         # Phase 6: deterministic score + thresholds + decision policy
│   │   ├── safety_gate.py     # Phase 7: ALLOW/BLOCK/ESCALATE policy engine (idempotent)
│   │   ├── action_executor.py # Phase 8: simulated recovery action execution (idempotent)
│   │   ├── outcome_simulator.py # Phase 8: scripted outcomes via event replay
│   │   ├── verification.py    # Phase 9: outcome verification + attribution rule (rules 12, 18-20)
│   │   ├── metrics.py         # Phase 9: Section 11 recovery metrics from stored data
│   │   ├── jobs.py            # Phase 11: durable background jobs, handler registry, expiry sweep
│   │   ├── scheduler.py       # Phase 11: in-process asyncio scheduler loop (default runner)
│   │   ├── demo.py            # deterministic demo-case selection from current DB state
│   │   └── agent/             # Phase 5: AI agent, context & controlled tools
│   │       ├── __init__.py
│   │       ├── tools.py       # 8 controlled tools: schemas, gating, agent_actions logging
│   │       ├── context.py     # structured context assembly + ambiguity assessment
│   │       ├── llm.py         # GLM 5.3 client (OpenAI-compatible tool calling)
│   │       └── orchestrator.py# run_agent: DETECTED→DIAGNOSING, diagnosis, escalation
│   ├── models/                # DB models
│   │   ├── base.py
│   │   ├── enums.py
│   │   ├── customers.py
│   │   ├── orders.py
│   │   ├── payments.py
│   │   ├── payment_events.py
│   │   ├── recovery_cases.py
│   │   ├── agent_actions.py
│   │   └── audit_logs.py
│   ├── database/              # migrations + seed
│   │   ├── migrations/
│   │   │   ├── env.py
│   │   │   ├── script.py.mako
│   │   │   └── versions/
│   │   │       ├── 0001_initial_schema.py
│   │   │       ├── 0002_payment_events.py
│   │   │       ├── 0003_attribution.py
│   │   │       └── 0004_background_jobs.py
│   │   └── seed.py            # deterministic synthetic dataset
│   └── tests/                 # pytest suite (real migrated schema on recoverai_test)
│       ├── conftest.py
│       ├── test_revenue_risk.py
│       ├── test_agent.py
│       ├── test_scoring.py
│       ├── test_safety_gate.py
│       ├── test_action_outcome.py
│       ├── test_verification_metrics.py
│       ├── test_jobs.py
│       ├── test_phase12_scenarios.py
│       └── test_demo_case.py
└── frontend/                  # Next.js merchant dashboard (Phase 10)
    ├── README.md              # how to run the dashboard
    ├── package.json           # next / react / tailwind + vitest + eslint scripts
    ├── next.config.ts         # /api/* proxy rewrites to the FastAPI backend
    └── src/
        ├── app/
        │   ├── layout.tsx     # shell: nav + simulated demo-mode banner
        │   ├── page.tsx       # dashboard: metric cards, status distribution, recent cases
        │   └── cases/
        │       ├── page.tsx   # case list: all required columns, status filter, pagination
        │       └── [id]/
        │           ├── page.tsx       # case detail: summary, attribution, context, controls
        │           └── not-found.tsx  # unknown case ids
        ├── components/        # MetricCard, CasesTable, Timeline, CaseControls,
        │                      # StatusBadge, ScoreBadge, SimulatedTag, StateViews
        └── lib/               # api.ts (typed backend client) + format.ts (display helpers)
```

---

## 6. Data Model

Eight PostgreSQL tables (migrations `0001_initial_schema`, `0002_payment_events`, `0003_attribution`, `0004_background_jobs`):

| Table | Columns |
|---|---|
| `customers` | `id` PK, `customer_id` (unique business id), `name`, `email`, `phone`, `lifetime_payments`, `lifetime_successes`, `prior_recovery_attempts`, `prior_recovery_successes`, `created_at` |
| `orders` | `id` PK, `order_id` (unique business id), `customer_id` FK → customers, `amount`, `currency`, `status` (`order_status`: CREATED / PAID / ATTEMPTED), `created_at` |
| `payments` | `id` PK, `payment_id` (unique business id), `order_id` FK → orders (nullable — missing order identity is an ambiguity trigger), `amount` (gateway-reported), `method` (UPI / CARD / NETBANKING / WALLET), `status` (`payment_status`: CREATED / PENDING / CAPTURED / FAILED), `failure_reason`, `gateway_metadata` (JSONB), `created_at`, `updated_at` |
| `recovery_cases` | `id` PK, `payment_id` FK → payments (source payment), `revenue_at_risk`, `diagnosis`, `score`, `selected_action`, `status` (`recovery_case_status` — the 11 states of Section 8), `attempt_count`, `expiry`, `recovered_payment_id` (unique — the payment credited by Phase 9 attribution), `recovered_amount`, `recovered_at`, `created_at`, `updated_at` |
| `agent_actions` | `id` PK, `case_id` FK → recovery_cases (nullable — context tools may run before a case exists), `tool_name`, `input` (JSONB), `output` (JSONB), `allowed` (true / false / null for ungated reads), `created_at` |
| `audit_logs` | `id` PK, `case_id` FK → recovery_cases (nullable — system-level events), `event_type`, `from_status`, `to_status`, `payload` (JSONB), `created_at` |
| `background_jobs` | `id` PK, `job_key` (unique — idempotent scheduling), `name` (registered handler: run_agent / verify_outcome / simulate_outcome / expiry_sweep), `params` (JSONB), `status` (PENDING / DONE / FAILED), `due_at`, `recurring_interval_seconds` (set → the job reschedules itself), `result` (JSONB), `error`, `created_at`, `updated_at` |
| `payment_events` | `id` PK, `event_id` (unique derived idempotency key), `source` (RAZORPAY_WEBHOOK / SYNTHETIC / REPLAY), `event_type`, `payment_ref`, `entity_status`, `raw_payload` (JSONB), `received_at`, `processed_at`, `processing_status` (PROCESSED / REJECTED / DUPLICATE), `reason`, `payment_id` FK → payments |

Schema-level enforcement of the Phase 1 rules:

- **One active case per payment:** partial unique index `uq_recovery_cases_active_per_payment` on `recovery_cases(payment_id)` where status is not terminal. A duplicate active case is rejected by the database itself; a terminal case plus a new active case is allowed.
- **Append-only audit trail:** a trigger rejects every `UPDATE` and `DELETE` on `audit_logs`.
- **Amount mismatch detection:** `orders.amount` is the authoritative order amount, `payments.amount` is the gateway-reported amount; comparing the two implements the amount-mismatch ambiguity trigger.
- **Event idempotency:** every accepted event derives a deterministic `event_id` (SHA-256 over event type, payment id, entity status, amount, error code and entity timestamp); the unique index on `payment_events.event_id` prevents duplicate processing, and redelivered events return `DUPLICATE` without side effects.
- **No double-counted recoveries (Phase 9):** `recovery_cases.recovered_payment_id` carries a unique index (`uq_recovery_cases_recovered_payment`) — the same successful payment can never be credited to two recovery cases, and a `RECOVERED` case is terminal so it can never be credited twice itself.
- **Payment state machine at intake:** `CAPTURED` and `FAILED` are terminal payment states — regressive updates are stored as `REJECTED` with reason `TERMINAL_STATE_CONFLICT` (current-state check).
- All timestamps are timezone-aware (`timestamptz`).

---

## 7. Agent Tools

Eight controlled tools (built in Phase 5, `backend/services/agent/tools.py`):

| Tool | Type | Purpose |
|---|---|---|
| `get_payment_status()` | Read | Current payment state — always checked before acting |
| `get_order_details()` | Read | Order identity, amount, currency, status |
| `get_customer_history()` | Read | Historical payment behaviour signals (contact details masked) |
| `calculate_recovery_score()` | Compute | Deterministic 0–100 recovery likelihood (Phase 6 scoring policy: six weighted factors, HIGH/MEDIUM/LOW bands, full explainable breakdown) |
| `create_recovery_payment()` | Act (gated) | Simulated recovery payment/link — executes only through the Phase 8 action executor after a gate ALLOW; every other call is blocked and logged |
| `send_recovery_notification()` | Act (gated) | Simulated notification with channel field (EMAIL / SMS / WHATSAPP) — recorded, never delivered; same executor-only execution rule |
| `check_recovery_result()` | Read | Verify the outcome of a recovery action (source payment, order, the simulated recovery payment created by the executor, and the Phase 9 attribution result: whether the case is credited as recovered and for how much) |
| `log_action()` | Record | Persist decision/tool result to the audit trail |

- The LLM selects tools; the backend decides whether a tool call is actually allowed. Unknown tools, invalid arguments and gated act tools are rejected or blocked server-side and still logged.
- Act tools execute **only through the Phase 8 action executor** after a fresh Phase 7 gate ALLOW (the executor passes the sole `authorized` flag): any other call — the LLM's, or a direct call on a case that never cleared the gate — is blocked with `GATE_AUTHORIZATION_REQUIRED`, recorded with `allowed = false` and never executed.
- Every tool call — allowed, blocked or errored — is stored in `agent_actions` with its structured input and output. The safety gate's decisions and the executor's runs are stored there too, alongside their audit events.
- Tool outputs are structured and stored.
- If required context is missing or conflicting, the agent returns an escalation recommendation instead of guessing.

---

## 8. Recovery Case State Machine

> **Phase 1 deliverable.** One clear lifecycle for every recovery case, defined before any UI, database integration or AI code. The backend must enforce exactly this machine.

### Case creation rule

A recovery case is created **only** when an incoming payment/order event represents revenue at risk:

- **Failed payment** with expected merchant revenue at risk → case created.
- **Pending payment** → verification first, never immediate recovery (Section 10).
- **Already-successful or already-reconciled payment** → no case.
- An **active case already exists** for the same underlying transaction/order → no duplicate case (idempotency).

*Implementation status:* enforced by the Phase 4 revenue-at-risk engine (`backend/services/revenue_risk.py`); ambiguous failures are created and immediately escalated per rule 17.

### States

| # | State | Kind | Meaning |
|---|---|---|---|
| 1 | `DETECTED` | Active | Revenue-at-risk event accepted; recovery case opened |
| 2 | `DIAGNOSING` | Active | Agent gathering context: transaction state, order details, customer history, prior attempts |
| 3 | `SCORED` | Active | Deterministic recovery score (0–100) computed and logged |
| 4 | `ACTION_SELECTED` | Active | Agent has recommended a recovery action |
| 5 | `SAFETY_CHECK` | Active | Policy gate is evaluating the proposed action (ALLOW / BLOCK / ESCALATE) |
| 6 | `ACTION_EXECUTED` | Active | Approved action executed: recovery record created, simulated payment/link generated, simulated notification sent |
| 7 | `WAITING_FOR_RESULT` | Active | Monitoring the outcome of the executed recovery action |
| 8 | `RECOVERED` | Terminal | Qualifying successful payment verified and attributed |
| 9 | `NOT_RECOVERED` | Terminal | All allowed attempts completed without a qualifying success |
| 10 | `STOPPED` | Terminal | Policy / time / retry limit reached |
| 11 | `ESCALATED` | Terminal | Ambiguous or exceptional case requiring human review |

A case in a terminal state can never transition again. A case in an active state cannot continue indefinitely — the 24-hour window and the retry cap force a terminal outcome.

### Diagram

```
DETECTED
   │  agent gathers context (payment status, order, customer history, prior attempts)
   ▼
DIAGNOSING ── context missing or conflicting ──────────▶ ESCALATED
   │  context complete; deterministic score computed and logged
   ▼
SCORED ── score below stop threshold ─────────────────▶ STOPPED
   │  agent recommends a recovery action
   ▼
ACTION_SELECTED ◀── BLOCK with safe alternative: re-select action ──┐
   │  action submitted to the policy gate                            │
   ▼                                                                 │
SAFETY_CHECK ── BLOCK / ESCALATE, no safe alternative ──▶ ESCALATED │
   │  gate returns ALLOW                                             │
   ▼                                                                 │
ACTION_EXECUTED                                                     │
   │  simulated recovery payment/link created,                       │
   │  simulated notification sent, monitoring begins                 │
   ▼                                                                 │
WAITING_FOR_RESULT ◀── retry cycle: outcome FAILED, ─────────────────┘
   │                     attempts < 2, within 24h window (→ DIAGNOSING)
   │
   ├─ outcome SUCCESS, verified + attributable ─▶ RECOVERED       (terminal)
   ├─ verified success, NOT attributable ───────▶ NOT_RECOVERED   (terminal, rule 18)
   ├─ outcome FAILED, attempts = 2 ─────────────▶ NOT_RECOVERED   (terminal)
   ├─ outcome STILL_PENDING / NO_RESPONSE ──────▶ keep monitoring (same state)
   └─ 24-hour case window expired ──────────────▶ STOPPED         (terminal)

From any active state:
   ├─ ambiguous or exceptional condition ─▶ ESCALATED (terminal)
   └─ policy / time / retry limit reached ─▶ STOPPED   (terminal)
```

*Implementation status:* rules 1–2 are enforced by the Phase 4 risk engine; rule 4 and the audited `transition()` layer are Phase 5; rules 3, 5 and 6 (score → `SCORED` → `ACTION_SELECTED`/`STOPPED`) are enforced by the Phase 6 scoring pipeline; rules 7–10 and gate-time expiry (rule 16) are enforced by the Phase 7 safety gate; rules 8 and 11 (execution → `ACTION_EXECUTED` → `WAITING_FOR_RESULT`) by the Phase 8 action executor, and rules 13–15 plus outcome-time expiry by the Phase 8 outcome simulator. Rule 12 (verified → `RECOVERED`) and rules 18–20 (non-attributable success, no double counting, verification-time expiry) are enforced by the Phase 9 verification service, which also produces the Section 11 metrics. The general expiry sweep of rule 16 runs as the recurring `expiry_sweep` background job of Phase 11, stopping every active case past its window from any active state.

### Policy constants

| Constant | Value | Enforced by |
|---|---|---|
| `MAX_RECOVERY_ATTEMPTS` | 2 active recovery attempts per case | Safety/policy gate |
| `CASE_WINDOW` | 24 hours from case creation | Expiry check |

### Auditability

Every state transition must be timestamped and auditable. The immutable `audit_logs` timeline records each transition, the decision that caused it and the resulting outcome.

---

## 9. State Transition Rules

Only the transitions below are legal. Any other transition is an unsupported state transition — the backend must reject it and treat it as an ambiguity trigger (escalation). Rules 18–20 were added in Phase 9 for verification and attribution; they reuse existing legal transitions (no new graph edges).

| # | From | Trigger | Guard | To |
|---|---|---|---|---|
| 1 | — | Verified failed payment event with revenue at risk | No active case exists for the same payment/order | `DETECTED` |
| 2 | `DETECTED` | Agent begins diagnosis | — | `DIAGNOSING` |
| 3 | `DIAGNOSING` | Context gathering complete | Required context present and consistent | `SCORED` |
| 4 | `DIAGNOSING` | Required context missing or conflicting | — | `ESCALATED` |
| 5 | `SCORED` | Score computed and logged | Score above stop threshold | `ACTION_SELECTED` |
| 6 | `SCORED` | Score computed and logged | Score below stop threshold | `STOPPED` |
| 7 | `ACTION_SELECTED` | Action submitted for validation | — | `SAFETY_CHECK` |
| 8 | `SAFETY_CHECK` | Policy gate returns ALLOW | — | `ACTION_EXECUTED` |
| 9 | `SAFETY_CHECK` | Policy gate returns BLOCK, safe alternative possible | Re-selection budget remains | `ACTION_SELECTED` |
| 10 | `SAFETY_CHECK` | Policy gate returns BLOCK/ESCALATE, no safe alternative | — | `ESCALATED` |
| 11 | `ACTION_EXECUTED` | Recovery action record + simulated payment/link + notification recorded | — | `WAITING_FOR_RESULT` |
| 12 | `WAITING_FOR_RESULT` | Outcome SUCCESS verified and attributable | Within case window, after an approved action | `RECOVERED` |
| 13 | `WAITING_FOR_RESULT` | Outcome FAILED | attempt_count < 2 and within case window | `DIAGNOSING` (new attempt) |
| 14 | `WAITING_FOR_RESULT` | Outcome FAILED | attempt_count = 2 | `NOT_RECOVERED` |
| 15 | `WAITING_FOR_RESULT` | Outcome STILL_PENDING or NO_RESPONSE | Within case window | `WAITING_FOR_RESULT` (keep monitoring) |
| 16 | Any active state | Case window (24h) expired | — | `STOPPED` |
| 17 | Any active state | Ambiguous or exceptional condition | See ambiguity triggers | `ESCALATED` |
| 18 | `WAITING_FOR_RESULT` | Verification finds a successful payment that is not attributable (captured before the approved action, outside the case window, or already credited to another case) | — | `NOT_RECOVERED` (the agent is not credited) |
| 19 | — | Attribution guard: the same recovered payment is never counted twice | Unique index on `recovery_cases.recovered_payment_id`; a `RECOVERED` case is terminal and never re-verified | — |
| 20 | `WAITING_FOR_RESULT` | Case window (24h) expired when verification runs (rule 16 at verification time) | — | `STOPPED` |

### Ambiguity triggers (escalation)

- Conflicting payment states
- Missing or invalid order identity
- Amount mismatch
- Duplicate or conflicting events
- Repeated uncertain failures
- Unsupported state transition
- Insufficient context

Ambiguous cases move to `ESCALATED` rather than allowing the AI to invent a recovery action.

### Terminal state definitions

- **RECOVERED** — a successful payment associated with the recovery case and occurring after an approved recovery action within the configured case window.
- **NOT_RECOVERED** — all allowed attempts completed without a qualifying success.
- **STOPPED** — policy / time / retry limit reached.
- **ESCALATED** — ambiguous or exceptional case requiring human review.

---

## 10. Pending-Payment Pre-Flow

For pending payments, the first action must always be verification rather than immediate recovery:

```
PENDING payment event
   │ verify current state (get_payment_status / check_recovery_result)
   ├── SUCCESS       → no recovery case; payment reconciled
   ├── STILL PENDING → monitor only; no recovery action
   └── FAILED        → revenue at risk → recovery case created → DETECTED
```

A pending payment that becomes successful before any recovery action never becomes a recovery case and is never credited to the agent.

*Implementation status:* the deterministic split is enforced by the Phase 4 revenue-at-risk engine — pending/created payments are routed to verification (audited as `risk.pending_verification`, no case), captured payments are ignored (`risk.already_successful`), and only failed payments enter the recovery workflow.

---

## 11. Attribution Rule and Metrics

### Attribution rule

- A success qualifies as **recovered** only if it is attributable to the recovery case: associated via order/payment identifiers, occurring after an approved recovery action, within the configured case window.
- If the customer independently retries outside the attributable action window, the agent is not automatically credited.
- The same recovered payment is never counted twice.

*Implementation status (Phase 9, `backend/services/verification.py`):* the rule is enforced deterministically by four checks evaluated for every successful payment on the case's order — **associated** (same order as the source payment; executor-created recovery payments also carry `gateway_metadata.recovery_case_id`), **after approved action** (the capture was observed after the case's earliest approved, executor-executed recovery action), **within case window** (capture no later than the case's 24-hour expiry), and **never twice** (not already credited to another case). The capture time is the moment the event pipeline observed the capture (the latest processed `payment.captured` event's `received_at`; the payment row's `updated_at` as fallback), so replayed and real webhook captures are treated identically. The credited payment, amount and timestamp are stored on the case (`recovered_payment_id`, `recovered_amount`, `recovered_at`), the unique index of Section 6 prevents double crediting (rule 19), and a verified success that fails attribution ends the case as `NOT_RECOVERED` without credit (rule 18).

### Metrics

| Metric | Definition |
|---|---|
| Revenue at risk | Sum of eligible amounts on revenue-at-risk cases |
| Recovered revenue | Sum of verified successful amounts attributable to completed recovery cases |
| Recovery rate | Recovered revenue ÷ total eligible revenue at risk |
| Attempts / successful recoveries | Recovery attempts and qualifying successes |
| Average recovery time | Mean time from case detection to verified recovery |
| Exception/escalation rate | Escalated cases ÷ total cases |
| Invalid or blocked actions | Actions blocked by the safety gate |

*Implementation status (Phase 9, `backend/services/metrics.py`, `GET /api/metrics`):* every metric is computed from stored data only — cases, attributed payments, agent actions and audit rows — never hardcoded or estimated. **Eligibility decision:** escalated cases are excluded from the eligible revenue at risk (and reported separately as `escalated_excluded`), because their ambiguity went to human review and the agent had no legitimate recovery opportunity on them; they are measured by the escalation rate. All other cases — active, stopped, not recovered and recovered — count as eligible. Average recovery time is the mean of `recovered_at − created_at` over `RECOVERED` cases, and invalid/blocked actions are all `agent_actions` rows with `allowed = false` (gate decisions and blocked/rejected tool calls), broken down by tool.

Illustrative demo target (final numbers must come from the actual test run): ₹5.40L revenue at risk, ₹2.46L recovered, 45.6% recovery rate.

### Evaluation dataset

A synthetic dataset of roughly 100–300 cases containing successful, failed, pending, abandoned and ambiguous payments, with varied amounts, methods, failure reasons and customer history. Scenarios are seeded deterministically so the final demo can be repeated.

---

## 12. Development Phases

The build follows the 12-phase sequence from the Implementation Decisions document. No phase is skipped; each phase completes before the next begins.

| Phase | Title | Status |
|---|---|---|
| 1 | Define the Recovery State Machine | COMPLETE |
| 2 | Database Schema & Synthetic Data | COMPLETE |
| 3 | Event Intake & Razorpay/Test Simulation | COMPLETE |
| 4 | Revenue-at-Risk Detection & Case Creation | COMPLETE |
| 5 | AI Agent, Context & Controlled Tools | COMPLETE |
| 6 | Recovery Scoring & Decision Policy | COMPLETE |
| 7 | Safety / Policy Gate | COMPLETE |
| 8 | Recovery Action & Outcome Simulator | COMPLETE |
| 9 | Verification & Attribution | COMPLETE |
| 10 | Dashboard, Metrics & Audit Timeline | COMPLETE |
| 11 | Async Processing & Queue Decision | COMPLETE |
| 12 | Testing, Demo & Definition of Done | COMPLETE |

### Phase 1 — Define the Recovery State Machine (COMPLETE)

**Objective:** create one clear lifecycle for every recovery case before building UI, database integrations or AI.

**Built:**
- Case creation rule: a recovery case is created only when the event represents revenue at risk (Section 8).
- Full state machine: `DETECTED → DIAGNOSING → SCORED → ACTION_SELECTED → SAFETY_CHECK → ACTION_EXECUTED → WAITING_FOR_RESULT` with terminal states `RECOVERED / NOT_RECOVERED / STOPPED / ESCALATED` (Sections 8–9).
- Pending payments always verify first, never recover immediately (Section 10).
- Every state transition is timestamped and auditable; terminal states are defined so a case cannot continue indefinitely.

**Deliverable:** a documented state machine and transition rules that the backend can enforce — Sections 8–10 of this README.

### Phase 2 — Database Schema & Synthetic Data (COMPLETE)

**Objective:** create the data foundation required for reasoning, tool calls, auditability and demo evaluation.

**Built:**
- Repository setup: FastAPI application skeleton (`backend/`) with a health endpoint, settings module (DB URL, locked safety constants, seed), SQLAlchemy engine/session, requirements, Alembic configuration, `.env.example`, `.gitignore`, and an optional `docker-compose.yml` for containerized PostgreSQL. The local development database runs on the machine's existing PostgreSQL 18 server as a dedicated `recoverai` role/database (credentials in `backend/.env`, gitignored).
- PostgreSQL schema in migration `0001_initial_schema`: the six tables of Section 6 with native enum types (`order_status`, `payment_status`, `recovery_case_status`) and indexes on business ids, statuses and foreign keys.
- Schema-level enforcement of the Phase 1 rules: the partial unique index permits at most one **active** recovery case per payment, and an append-only trigger rejects any `UPDATE`/`DELETE` on `audit_logs`.
- Deterministic seed script `backend/database/seed.py` (seed = 42, fixed window ending 2026-08-30T18:00Z): **80 customers, 227 orders, 236 payments** — 70 captured, 121 failed (100 cleanly eligible + 21 ambiguous: 5 amount mismatch, 4 conflicting order/payment state, 3 missing order identity, 9 repeated uncertain failures over 3 orders), 25 pending, 20 abandoned (checkout started, never completed). Amounts (₹100–₹30,000 in four bands), payment methods, failure reasons, customer behaviour tiers (good / average / risky with prior recovery attempts) and timestamps all vary deterministically.
- `recovery_cases`, `agent_actions` and `audit_logs` are created empty on purpose — they are produced by the pipeline in Phases 3–9 when events flow through the system.

**Verified:**
- Migration upgrade and downgrade run cleanly; after a downgrade/upgrade/reseed cycle the data is byte-identical.
- Seeding is repeatable: identical md5 checksums for `customers`, `orders` and `payments` across truncate + reseed runs.
- Duplicate active recovery case per payment is rejected by the database (`IntegrityError`).
- A terminal case plus a new active case for the same payment is allowed.
- Audit log `UPDATE` and `DELETE` are rejected by the append-only trigger.
- `GET /health` returns `{"status": "ok", "app": "RecoverAI"}`.

**How to run** (from the repository root):

```bash
# 1. PostgreSQL (choose one)
docker compose up -d                                    # containerized
# or use an existing local PostgreSQL and set DATABASE_URL in backend/.env

# 2. Backend environment
cp .env.example backend/.env
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt           # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Unix

# 3. Schema + data
.venv\Scripts\alembic upgrade head
.venv\Scripts\python -m database.seed                   # add --reset to truncate and reseed

# 4. API health check
.venv\Scripts\uvicorn main:app --reload                 # http://localhost:8000/health
```

**Deliverable:** PostgreSQL schema, migrations, seed script and repeatable synthetic dataset — done.

### Phase 3 — Event Intake & Razorpay/Test Simulation (COMPLETE)

**Objective:** build a reliable entry point for payment events and make event processing testable.

**Built:**
- `payment_events` table (migration `0002_payment_events`): every accepted event is stored raw with its source, derived idempotency key, processing outcome and machine-readable reject reason.
- Event intake service (`backend/services/event_intake.py`): structural validation (malformed events → HTTP 422, not stored), normalization into the internal payment model (paise → rupees, Razorpay status mapping, failure-reason taxonomy mapping), semantic checks (unsupported event type, unmappable status, event/entity status mismatch → stored as `REJECTED`), idempotency via the derived `event_id` (redeliveries return `DUPLICATE` without reprocessing), payment upsert under the intake state machine (`CAPTURED`/`FAILED` terminal — regressive updates rejected as `TERMINAL_STATE_CONFLICT`), order status sync (captured → `PAID`, failed on a `CREATED` order → `ATTEMPTED`), audit log entries (`payment.created`, `payment.status_changed`, `order.status_changed`, `pipeline.dispatched`), and the dispatch point where the Phase 4 revenue-at-risk rules plug in.
- `POST /webhooks/razorpay`: accepts Razorpay-shaped webhook envelopes; verifies the `X-Razorpay-Signature` header (HMAC-SHA256 of the raw body) when `RAZORPAY_WEBHOOK_SECRET` is configured — invalid or missing signatures are rejected with 401; when the secret is empty (development mode) the event is accepted and reported with `signature_verified: false`.
- `POST /api/events/synthetic` and `POST /api/events/replay`: locally generated events so the demo never depends on external payment timing. Both accept a simple spec (`payment_id`, `event`, optional `amount_paise`, `method`, `order_id`, `error_code`, `error_description`, `created_at`), expand it into a full Razorpay-shaped envelope using stored payment data where fields are omitted, and run it through the exact same pipeline (source is labelled `SYNTHETIC` or `REPLAY`). Passing an explicit `created_at` makes repeated calls idempotent. The replay endpoint is the scripted mechanism used to inject success/failure outcomes during demo and testing (used by the Phase 8 outcome simulator).
- `GET /api/events` and `GET /api/events/{event_id}`: inspect stored raw events and their processing outcomes.
- Supported event types: `payment.captured`, `payment.failed`, `payment.authorized` (→ PENDING), `payment.pending` (synthetic extension for pending simulation).

**Event lifecycle:** raw webhook/synthetic envelope → structural validation → stored in `payment_events` → normalized (amount, status, failure reason) → duplicate check → payment created or updated (state-machine guarded, current-state checked) → order status sync → audit log → dispatched to the recovery case pipeline (Phase 4 implements the eligibility rules at this dispatch point).

**Verified (live, against the seeded database):**
- Pending → captured via synthetic event: payment and order updated, `PROCESSED`.
- Failed event on a captured payment: stored `REJECTED` / `TERMINAL_STATE_CONFLICT`, payment untouched.
- Identical synthetic event posted twice (fixed `created_at`): second delivery returns `DUPLICATE`; exactly one `payment_events` row, no double side effects.
- Raw webhook envelope with a new payment id on a previously-failed order: payment created (₹19,983.00 from 1998300 paise, method `upi` → `UPI`), order → `PAID`; subsequent failed event for the same payment → `TERMINAL_STATE_CONFLICT`.
- Malformed envelope (missing payload): HTTP 422, nothing stored.
- Unsupported event type (`refund.processed`): stored `REJECTED` / `UNSUPPORTED_EVENT_TYPE`.
- Replay endpoint: pending payment → failed with `error_description: "UPI transaction timed out at bank"` → failure reason mapped to `BANK_TIMEOUT`.
- Event list and detail endpoints; unknown event id → 404.
- Audit trail consistent with outcomes: 1 `payment.created`, 3 `payment.status_changed`, 4 `order.status_changed`, 4 `pipeline.dispatched` for the 4 processed events.
- With `RAZORPAY_WEBHOOK_SECRET` set: valid HMAC signature → `PROCESSED` with `signature_verified: true`; invalid signature → 401; missing signature → 401.
- After verification the database was reset to the pristine seeded state (`python -m database.seed --reset`, which now also truncates `payment_events`).

**How to exercise the intake** (server running via `uvicorn main:app`):

```bash
# synthetic event (simple spec, expanded to a Razorpay-shaped envelope)
curl -X POST http://localhost:8000/api/events/synthetic \
  -H "Content-Type: application/json" \
  -d '{"payment_id":"pay_0181","event":"payment.captured"}'

# scripted replay of a failure outcome
curl -X POST http://localhost:8000/api/events/replay \
  -H "Content-Type: application/json" \
  -d '{"payment_id":"pay_0185","event":"payment.failed","error_description":"Insufficient funds"}'

# raw Razorpay-shaped webhook (full envelope; signed when a secret is configured)
curl -X POST http://localhost:8000/webhooks/razorpay \
  -H "Content-Type: application/json" \
  -d '{"entity":"event","event":"payment.captured","contains":["payment"],"payload":{"payment":{"entity":{"id":"pay_evt_9001","entity":"payment","amount":1998300,"currency":"INR","status":"captured","order_id":"order_0149","method":"upi","created_at":1787000100}}}}'

# inspect stored events
curl http://localhost:8000/api/events
```

**Deliverable:** event → normalized payment record → recovery case creation pipeline — done (the pipeline dispatch point is in place; Phase 4 defines which events become recovery cases).

### Phase 4 — Revenue-at-Risk Detection & Case Creation (COMPLETE)

**Objective:** decide which incoming events deserve a recovery case.

**Built:**
- Revenue-at-risk engine (`backend/services/revenue_risk.py`), invoked by the event pipeline after every processed event, evaluating the payment's *current* state with deterministic rules:
  - **CAPTURED payments never create cases** (already successful / reconciled) — audited as `risk.already_successful`, decision `ALREADY_SUCCESSFUL_IGNORED`.
  - **PENDING and CREATED payments never create cases** — routed to verification first (Section 10) — audited as `risk.pending_verification`, decision `PENDING_VERIFICATION`. When such a payment later fails, the normal failed-payment rules apply.
  - **FAILED payments create a case in `DETECTED`** with `revenue_at_risk` = the authoritative order amount (the payment amount when no order exists), `attempt_count = 0` and `expiry = now + 24 h` (the configured case window).
- **Idempotency at transaction/order level:** no new case when an active case already exists for the same payment *or* for any payment on the same order (decision `DUPLICATE_ACTIVE_CASE`, audited as `risk.case_duplicate`). This also covers recovery-attempt payments created by later phases. Terminal cases do **not** block new cases — a new failure after a terminal case is new revenue at risk (verified against the Phase 2 partial unique index behaviour).
- **Ambiguity triggers detected deterministically at creation time**, in priority order: missing order identity (`AMBIGUOUS_MISSING_ORDER`), conflicting order/payment state — order `PAID` while payment `FAILED` (`AMBIGUOUS_CONFLICTING_STATE`), amount mismatch — payment amount ≠ order amount (`AMBIGUOUS_AMOUNT_MISMATCH`), and repeated uncertain failures — ≥ 3 FAILED payments with `NETWORK_ERROR` or `BANK_TIMEOUT` on the same order (`AMBIGUOUS_REPEATED_UNCERTAIN`, threshold configurable via `repeated_uncertain_failure_threshold`). Ambiguous cases are created in `DETECTED` and immediately transitioned to `ESCALATED` (state machine rule 17) with the reason recorded, so they can never reach a recovery action. The repeated-uncertain trigger also escalates an *existing* active case when the pattern completes on a later failure event.
- **Every decision is returned in the API response** as `risk_evaluation` (`CASE_CREATED` / `CASE_ESCALATED` / `DUPLICATE_ACTIVE_CASE` / `PENDING_VERIFICATION` / `ALREADY_SUCCESSFUL_IGNORED`) with `case_id`, `reason` and `revenue_at_risk`, and audited (`risk.case_created`, `risk.case_escalated`, `risk.case_duplicate`, `risk.pending_verification`, `risk.already_successful`). These events supersede the Phase 3 `pipeline.dispatched` audit entry.
- Escalated cases still record their revenue at risk; the Phase 9 attribution rules decided that they are **excluded** from the eligible revenue at risk in the metrics and reported separately (Section 11).
- Checkout abandonment, subscription failures and overdue receivables remain out of this loop (extension / out of MVP scope).
- A shared audit writer (`backend/services/audit.py`) is now used by both the intake and risk services.

**Verified:**
- Pytest suite `backend/tests/test_revenue_risk.py` — 13 tests against the real migrated schema on a dedicated `recoverai_test` database (schema reset via Alembic, tables truncated per test):
  1. clean failed payment creates a `DETECTED` case with correct amount, 24 h expiry and audit rows
  2. a distinct failed event for the same payment is a duplicate (no second case)
  3. a failed payment on the same order while a case is active is a duplicate
  4. pending payment routes to verification (no case, audited)
  5. captured payment never creates a case (order reconciled to `PAID`)
  6. missing order identity → born-escalated case with two audited transitions
  7. amount mismatch → born-escalated case
  8. conflicting order/payment state → born-escalated case
  9. repeated uncertain failures escalate the active case on the third failure
  10. repeated uncertain failures present at creation escalate immediately
  11. a new case is allowed after a terminal case (terminal + active coexistence)
  12. event-level duplicates return `DUPLICATE` without a risk evaluation (no double side effects)
  13. the HTTP endpoint response includes the `risk_evaluation` object (FastAPI TestClient)
- Live smoke test on the development database with seed data: a failed event for a clean seed payment created case #1 (`DETECTED`, ₹19,983.00 at risk, 24 h expiry); a second distinct failure returned `DUPLICATE_ACTIVE_CASE`; an authorized event for a pending payment returned `PENDING_VERIFICATION`; `recovery_cases` and `audit_logs` contents verified via SQL; the database was then reset to the pristine seeded state.

**How to run the tests** (from `backend/`, with the `recoverai_test` database created once):

```bash
.venv\Scripts\python -m pytest
```

**Deliverable:** deterministic rules that convert eligible payment events into recovery cases — done.

### Phase 5 — AI Agent, Context & Controlled Tools (COMPLETE)

**Objective:** implement the AI reasoning layer as an orchestrator over structured backend tools, not as an unrestricted chatbot.

**Built:**
- **State machine enforcement** (`backend/services/case_lifecycle.py`): the full legal-transition table of Section 9 in one place; every case status change now goes through `transition()`, which audits the change and raises on any unsupported transition (treated as an ambiguity trigger).
- **Controlled tool layer** (`backend/services/agent/tools.py`): the eight tools of Section 7, each with an explicit JSON schema and a deterministic backend handler. Read/compute tools execute server-side; act tools (`create_recovery_payment`, `send_recovery_notification`) are argument-validated and **blocked by the backend** (`BLOCKED_BY_BACKEND` / `SAFETY_GATE_PENDING_PHASE_7`) until the Phase 7 gate and Phase 8 executor exist — the LLM can request them, the request is logged with `allowed = false`, and nothing executes. Unknown tools and invalid arguments are rejected the same way. `calculate_recovery_score` already returns the complete score-input set with `score: null` (the deterministic score is Phase 6). `get_customer_history` masks email/phone before anything leaves the backend.
- **Structured context builder** (`backend/services/agent/context.py`): the backend — not the LLM — assembles the case context by executing `get_payment_status`, `get_order_details` and `get_customer_history` through the tool layer (so every context lookup is logged), adds prior attempts (payment history on the order, prior recovery cases) and runs a deterministic ambiguity assessment (missing order identity, payment state changed, conflicting order state, amount mismatch, repeated uncertain failures). Customer PII is masked.
- **GLM 5.3 structured tool calling** (`backend/services/agent/llm.py`): OpenAI-compatible `/chat/completions` client configured via `AGENT_LLM_BASE_URL` / `AGENT_LLM_API_KEY` / `AGENT_LLM_MODEL` (default `agentrouter/glm-5.3`). The system prompt encodes the operating rules (recommend only from the approved catalog, never guess, cannot bypass the gate).
- **Agent orchestration** (`backend/services/agent/orchestrator.py`, `run_agent`): `DETECTED → DIAGNOSING` (audited), context assembly, then either escalation (ambiguous context → `ESCALATED` via rule 4, with a structured escalation diagnosis) or a structured diagnosis stored on the case. In LLM mode a bounded tool-calling loop (max `AGENT_MAX_TOOL_CALLS` turns) lets the model investigate with the read tools and deliver its result via a forced `submit_diagnosis` tool call whose arguments are validated server-side against the approved action catalog — an invalid or invented recommendation is rejected and falls back to the deterministic diagnosis. With no LLM configured (or on any LLM transport/validation failure) a deterministic rule-based fallback produces the diagnosis (`reasoning_source: rule_based_fallback`), so tests and offline demos never depend on external availability. `recommended_action` is one of `RETRY_PAYMENT_LINK / SEND_NOTIFICATION_ONLY / WAIT_AND_MONITOR / ESCALATE`; a recommendation to escalate does not by itself move the case — scoring, selection and execution are Phases 6–8.
- **Tool/action logging:** every tool call (context reads, LLM-requested calls, blocked act attempts, the submitted diagnosis) is persisted to `agent_actions` with structured input/output and `allowed` (null = ungated read, true = executed, false = blocked/rejected); agent lifecycle events are audited as `agent.diagnosis_started`, `agent.diagnosis_completed`, `agent.case_escalated`, `agent.note`.
- **Case APIs** (`backend/api/cases.py`): `GET /api/cases` (filter by status), `GET /api/cases/{id}` (case + payment/order/customer + the full `agent_actions` and `audit_logs` timeline) and `POST /api/cases/{id}/agent/run` (runs the agent; 404 unknown case, 409 illegal transition). Automatic agent invocation on case creation is implemented by Phase 11's background processing (`agent:{case}:0` job, run by the scheduler or `POST /api/jobs/run`).
- Env template updated (`.env.example`) with the agent LLM settings; no new dependencies.

**Verified:**
- Pytest suite `backend/tests/test_agent.py` — 20 tests (suite total 33, all passing against the real migrated schema on `recoverai_test`):
  1. clean case: `DETECTED → DIAGNOSING`, diagnosis stored, recommendation within the catalog, exact `agent_actions` sequence (3 read tools + `submit_diagnosis`) with structured outputs, audit trail `risk.case_created → agent.diagnosis_started → agent.diagnosis_completed`
  2. fallback recommendation thresholds (strong/mixed/weak customer history → `RETRY_PAYMENT_LINK` / `SEND_NOTIFICATION_ONLY` / `WAIT_AND_MONITOR`)
  3. `RISK_BLOCKED` failure → `ESCALATE` recommendation with reason (case stays `DIAGNOSING` — a recommendation, not a transition)
  4. conflicting context (payment state changed) → case `ESCALATED` with `PAYMENT_STATE_CHANGED`
  5. amount-mismatch drift → case `ESCALATED` with `AMOUNT_MISMATCH`
  6. terminal case → no-op, no side effects
  7. non-diagnosable status (`SCORED`) → no-op with reason
  8. act tools blocked by the backend (blocked, logged `allowed = false`; invalid channel → `INVALID_ARGUMENTS`)
  9. act tool on a terminal case → blocked with `TERMINAL_CASE`
  10. unknown tool → `UNKNOWN_TOOL`, logged `allowed = false`
  11. `log_action` → `agent.note` audit entry; missing note rejected
  12. `check_recovery_result` → current payment/order state, `recovery_action_executed: false`
  13. `calculate_recovery_score` → `score: null` with the full input set, Phase 6 note
  14. `get_customer_history` masks email and phone (raw PII absent from the output)
  15. LLM mode (mocked GLM): extra tool call executed + `submit_diagnosis` accepted, `reasoning_source` = configured model
  16. LLM recommends an action outside the catalog → rejected, deterministic fallback with `failed backend validation` note
  17. LLM transport error → deterministic fallback with `GLM call failed` note
  18. unknown case id → `CaseNotFoundError`
  19. state machine rejects unsupported and terminal-state transitions
  20. HTTP endpoints: agent run 200/404, case list + status filter (422 on unknown status), case detail with the full tool/audit timeline
- Live smoke test on the development database (server via `uvicorn main:app`): synthetic failed event for seed payment `pay_0077` (UPI, `INSUFFICIENT_FUNDS`, ₹257.00) → case #1 created → agent run returned `DIAGNOSIS_COMPLETED` with `RETRY_PAYMENT_LINK` (HIGH confidence, 88% customer success rate) and the three logged context tools; seed payment `pay_0072` (`RISK_BLOCKED`) → `ESCALATE` recommendation with escalation reason; `GET /api/cases/1` showed the complete timeline (`get_payment_status` / `get_order_details` / `get_customer_history` ungated, `submit_diagnosis` allowed, audit transitions `→ DETECTED → DIAGNOSING`). The database was reset to the pristine seeded state afterward.

**How to run the tests** (from `backend/`):

```bash
.venv\Scripts\python -m pytest
```

**How to run the agent** (server running via `uvicorn main:app`):

```bash
# create a case (synthetic failed payment), then run the agent on it
curl -X POST http://localhost:8000/api/events/synthetic \
  -H "Content-Type: application/json" \
  -d '{"payment_id":"pay_0077","event":"payment.failed","error_description":"Insufficient funds"}'

curl -X POST http://localhost:8000/api/cases/1/agent/run

# inspect the case with its tool-call and audit timeline
curl http://localhost:8000/api/cases/1
```

To enable GLM 5.3 structured tool calling, set `AGENT_LLM_BASE_URL` and `AGENT_LLM_API_KEY` in `backend/.env` (OpenAI-compatible endpoint); with them empty the agent runs its deterministic rule-based fallback.

**Deliverable:** AI agent that can reason over a case and produce structured tool/action decisions — done.

### Phase 6 — Recovery Scoring & Decision Policy (COMPLETE)

**Objective:** define a transparent recovery likelihood score so actions are not chosen randomly.

**Built:**
- Deterministic scoring service (`backend/services/scoring.py`): a weighted 0–100 score over exactly the six README inputs, with every factor, weight and point contribution returned for explainability:

  | Factor | Weight | Value mapping |
  |---|---|---|
  | `failure_reason` | 25 | retryability: INSUFFICIENT_FUNDS 1.0 · BANK_TIMEOUT / NETWORK_ERROR 0.8 · AUTHENTICATION_FAILED / LIMIT_EXCEEDED 0.5 · CARD_DECLINED 0.4 · INVALID_VPA 0.3 · RISK_BLOCKED 0.0 · anything else 0.5 |
  | `customer_success_rate` | 30 | lifetime successes ÷ lifetime payments (0 when no history) |
  | `method` | 10 | UPI 0.9 · CARD 0.8 · NETBANKING 0.7 · WALLET 0.6 · else 0.5 |
  | `amount` | 10 | ≤ ₹500 1.0 · ≤ ₹2,000 0.8 · ≤ ₹10,000 0.6 · else 0.4 |
  | `prior_attempts` | 10 | customer `prior_recovery_attempts` + case `attempt_count`: 1 − 0.2 per attempt, floored at 0 |
  | `recency` | 15 | hours since the last payment state change: 1.0 fresh, decaying linearly to 0.2 at 24 h |

  The weights sum to 100 and the score is clamped to 0–100. The score is decision support, not proof of success; a learned model may replace the heuristic later without changing the interface.
- Thresholds and bands (configurable via `SCORE_HIGH_THRESHOLD` = 80, `SCORE_STOP_THRESHOLD` = 35): `score >= high` → HIGH, `stop <= score < high` → MEDIUM, `score < stop` → LOW.
- Decision policy (`decide_action`): combines the band with the agent's diagnosis recommendation deterministically —
  - recommendation `ESCALATE` → the case is escalated after scoring (rule 17; the score is recorded but not actioned);
  - LOW band → the case is stopped (rule 6), no action selected;
  - MEDIUM band + `RETRY_PAYMENT_LINK` → downgraded to the cautious `SEND_NOTIFICATION_ONLY`;
  - otherwise the recommended action becomes `selected_action` (HIGH band = eligible for the full action, `PROCEED`; medium/cautious recommendations are kept, `CAUTIOUS`).
- `calculate_recovery_score` tool finalized: returns the score, band, thresholds, the full factor breakdown (input → factor → weight → points) and the raw inputs; every call is logged to `agent_actions` as before.
- Pipeline integration (`backend/services/agent/orchestrator.py`): after the diagnosis, the agent run executes `calculate_recovery_score` through the controlled tool layer, stores the score on the case, transitions `DIAGNOSING → SCORED` (audited as `agent.scored` with score, band and thresholds) and applies the decision policy — `SCORED → ACTION_SELECTED` (audited as `agent.action_selected` with the selected action and the reason), `SCORED → STOPPED` (`agent.case_stopped`) or `SCORED → ESCALATED` (`agent.case_escalated`). A failure to compute the score is itself an ambiguity trigger: the case escalates rather than being actioned. `agent.diagnosis_completed` is audited between diagnosis and scoring, so the full timeline reads: `risk.case_created → agent.diagnosis_started → agent.diagnosis_completed → agent.scored → agent.action_selected / agent.case_stopped / agent.case_escalated`.
- Safety controls preserved: act tools remain blocked (`SAFETY_GATE_PENDING_PHASE_7`), the LLM can only recommend, every state change goes through the audited transition table, and terminal cases never re-run. The Phase 7 gate evaluates the selected action next.

**Verified:**
- Pytest suite `backend/tests/test_scoring.py` — 10 tests: band boundaries (80/79/35/34), every factor mapping (failure-reason taxonomy, amount bands at 500/501/2000/2001/10000/10001, methods, prior-attempt decay and clamp, recency decay and 24 h floor), weights summing to 100, the complete decision-policy matrix (ESCALATE in any band, LOW → STOP in any band, MEDIUM downgrade, HIGH proceed, cautious recommendations kept), a full low-score integration case (weak customer, aged payment, large amount → score 30 → `STOPPED`, terminal, re-run is a no-op) and the attempt-count penalty (attempt_count = 2 reduces the clean-case score 94 → 90 with the `prior_attempts` factor visible).
- `backend/tests/test_agent.py` updated for the Phase 6 pipeline (suite total 43, all passing): clean case → score 94 / HIGH / `ACTION_SELECTED` with `selected_action = RETRY_PAYMENT_LINK`, the exact `agent_actions` sequence (3 context reads + `submit_diagnosis` + `calculate_recovery_score`) and audit timeline including `agent.scored` and `agent.action_selected`; history tiers → 94/HIGH `RETRY_PAYMENT_LINK`, 79/MEDIUM `SEND_NOTIFICATION_ONLY`, 73/MEDIUM `WAIT_AND_MONITOR`; `RISK_BLOCKED` → score 69, escalation after scoring with the `agent.case_escalated` transition from `SCORED`; LLM-mode (mocked GLM) and both fallback paths now flow through scoring to `ACTION_SELECTED`; the HTTP endpoints expose `score`, `selected_action`, the factor breakdown and the extended timelines.
- Live smoke test on the development database (server via `uvicorn main:app`): seed payment `pay_0077` (UPI, ₹257, INSUFFICIENT_FUNDS, 88% customer success rate) → case #1 → agent run → **score 96, HIGH, `RETRY_PAYMENT_LINK`, `ACTION_SELECTED`** with the full factor breakdown (`25 + 26.54 + 9 + 10 + 10 + 15`); seed payment `pay_0186` (50% success rate, ₹13,457, 2 prior attempts) → **score 74, MEDIUM, `RETRY_PAYMENT_LINK` recommendation downgraded to `SEND_NOTIFICATION_ONLY`**; seed payment `pay_0191` (strong customer, risk-blocked failure) → **score 70, MEDIUM, `ESCALATED`** after scoring. Case detail timelines verified; the database was reset to the pristine seeded state afterward.

**How to run the tests** (from `backend/`):

```bash
.venv\Scripts\python -m pytest
```

**How to score a case** (server running via `uvicorn main:app`):

```bash
# create a case, then run the agent: diagnosis → score → decision in one run
curl -X POST http://localhost:8000/api/events/synthetic \
  -H "Content-Type: application/json" \
  -d '{"payment_id":"pay_0077","event":"payment.failed","error_description":"Insufficient funds"}'

curl -X POST http://localhost:8000/api/cases/1/agent/run
# → {"decision":"ACTION_SELECTED","score":{"score":96,"band":"HIGH","factors":[...]},"selection":{"selected_action":"RETRY_PAYMENT_LINK","decision":"PROCEED",...}}

curl http://localhost:8000/api/cases/1     # score, selected_action and the full timeline
```

**Deliverable:** recovery score function + thresholds + explainable decision output — done.

### Phase 7 — Safety / Policy Gate (COMPLETE)

**Objective:** ensure every proposed action passes deterministic backend validation before execution.

**Built:**
- Policy engine (`backend/services/safety_gate.py`) returning **ALLOW / BLOCK / ESCALATE** with a machine-readable reason and the full check list. `evaluate()` is a pure function; `submit_to_gate()` applies the outcome through the audited state machine. Checks, evaluated in a fixed order (first failure decides):
  1. `case_window` — the 24-hour case window must not have expired (rule 16 at submission time): expired → BLOCK `CASE_WINDOW_EXPIRED`, terminal `STOPPED`
  2. `action_catalog` — the selected action must be an actionable catalog entry (`RETRY_PAYMENT_LINK` / `SEND_NOTIFICATION_ONLY` / `WAIT_AND_MONITOR`): otherwise ESCALATE `NO_VALID_ACTION`
  3. `payment_state` — the original transaction state is re-verified: payment CAPTURED → ESCALATE `PAYMENT_ALREADY_SUCCESSFUL` (actions on already-successful payments are blocked; the conflict goes to human review); payment not FAILED with an act action selected → BLOCK `PAYMENT_NOT_FAILED` with safe alternative `WAIT_AND_MONITOR` (pending payments verify first, Section 10); monitoring a pending payment is allowed
  4. `order_identity` — the order must be linked: missing → ESCALATE `MISSING_ORDER_IDENTITY`
  5. `amount_match` — gateway amount must equal the authoritative order amount: mismatch → ESCALATE `AMOUNT_MISMATCH`
  6. `order_state` — order PAID while payment FAILED → ESCALATE `CONFLICTING_ORDER_STATE`
  7. `duplicate_active_case` — no other active recovery case on the same order: duplicate → ESCALATE `DUPLICATE_ACTIVE_CASE`
  8. `attempt_limit` — act actions only: `attempt_count < MAX_RECOVERY_ATTEMPTS` (2); at the limit → BLOCK `ATTEMPT_LIMIT_REACHED` with safe alternative `WAIT_AND_MONITOR` while re-selection budget remains (monitoring is not an attempt and skips this check)
- **BLOCK with safe alternative (rule 9):** the case returns to `ACTION_SELECTED` with the alternative selected (audited as `gate.blocked`); the re-selection budget (`GATE_RESELECTION_BUDGET` = 1) bounds how often a BLOCKed action may be re-selected — once exhausted, a further BLOCK escalates instead.
- **ESCALATE (rules 10/17):** ambiguity triggers move the case to `ESCALATED` (`gate.escalated`) rather than letting the AI invent an action. A BLOCK without a safe alternative escalates the same way.
- **ALLOW (rule 8 precondition):** the decision is logged (`gate.allowed`) and the case parks in `SAFETY_CHECK` awaiting the Phase 8 action executor — nothing has executed at this point.
- **Idempotency keys:** each submission derives a deterministic key (`gate:{case}:{action}:{attempt_count}`); re-submitting the same action returns the recorded decision verbatim (flagged `replay: true`) with no re-evaluation, no new `agent_actions` row and no new audit event — a redelivered or retried submission can never double-decide.
- **Logging both allowed and blocked actions:** every gate decision is stored in `agent_actions` (tool name `safety_gate`, `allowed` = true only for ALLOW) with its structured input (key + action) and output (decision, reason, checks), plus the audit events `gate.submitted` (`ACTION_SELECTED → SAFETY_CHECK`, rule 7), `gate.allowed`, `gate.blocked`, `gate.escalated`, `gate.case_stopped`.
- **Pipeline integration:** `run_agent` now continues past action selection — after `ACTION_SELECTED` the selected action is submitted to the gate automatically (bounded by the re-selection budget: a BLOCKed act action is re-selected to its alternative and resubmitted once). The agent run therefore ends in `SAFETY_CHECK` (allowed, awaiting Phase 8), `ACTION_SELECTED` (only if the gate could not run), `STOPPED` (window expired), or `ESCALATED`. The act-tool block reason in the tool layer was updated to `ACTION_EXECUTOR_PENDING_PHASE_8`: the gate exists, the executor does not yet.
- **API:** `POST /api/cases/{id}/gate/evaluate` submits the selected action to the gate (idempotent; 404 unknown case, 200 with the decision or NOOP for non-submittable/terminal cases).
- The action catalog constants (`APPROVED_ACTIONS` and friends) now live in the gate module and are re-exported by the tool layer, so the LLM prompt, the diagnosis validation and the gate validate against one source of truth.

**Verified:**
- Pytest suite `backend/tests/test_safety_gate.py` — 18 tests against the real migrated schema: ALLOW happy path with the exact eight-check list, idempotent replay (single `safety_gate` row, single `gate.allowed` audit on double submission), attempt-limit BLOCK → `WAIT_AND_MONITOR` alternative → resubmission ALLOW (attempt-limit check SKIP for monitoring), re-selection budget exhaustion → escalation, captured payment → `PAYMENT_ALREADY_SUCCESSFUL` escalation, pending payment + act action → BLOCK with alternative while monitoring a pending payment is allowed, missing order identity / amount mismatch / conflicting order state / duplicate active case → the matching escalations, window expiry → `STOPPED` (`CASE_WINDOW_EXPIRED`), invalid action → `NO_VALID_ACTION` escalation, NOOP on non-submittable and terminal cases (no side effects), full `run_agent` pipeline ending in `SAFETY_CHECK` with the complete seven-event audit timeline, `run_agent` with an exhausted attempt budget performing the BLOCK → re-select → ALLOW cycle in one run, and the `POST /gate/evaluate` endpoint (404 / ALLOW / replay / timeline).
- `backend/tests/test_agent.py` updated for the gated pipeline (suite total 61, all passing): the clean case now ends `SAFETY_CHECK` with `gate.decision = ALLOW`, `safety_gate` appears in `agent_actions` (allowed) and `tool_calls`, and the audit timeline extends to `gate.submitted → gate.allowed`; the fallback tiers and LLM-mode/fallback tests all run through the gate; the act-tool block reason assertion updated to `ACTION_EXECUTOR_PENDING_PHASE_8`; the HTTP endpoint test asserts the extended timelines and the gate result in the run response.
- Live smoke test on the development database (server via `uvicorn main:app`): seed payment `pay_0077` → case #1 → agent run → **score 96, HIGH, `RETRY_PAYMENT_LINK`, gate ALLOW, case `SAFETY_CHECK`** with the full timeline `risk.case_created → agent.diagnosis_started → agent.diagnosis_completed → agent.scored → agent.action_selected → gate.submitted → gate.allowed` and `safety_gate` logged `allowed = true`; `POST /api/cases/1/gate/evaluate` returned the same ALLOW as an idempotent `replay: true`; seed payment `pay_0186` (medium band) → cautious `SEND_NOTIFICATION_ONLY` → gate ALLOW → `SAFETY_CHECK`. The database was reset to the pristine seeded state afterward.

**How to run the tests** (from `backend/`):

```bash
.venv\Scripts\python -m pytest
```

**How to run the gate** (server running via `uvicorn main:app`):

```bash
# the agent run now includes the gate: diagnosis → score → selection → gate in one call
curl -X POST http://localhost:8000/api/cases/1/agent/run
# → {"decision":"SAFETY_CHECK","gate":{"decision":"ALLOW","gate":{"reason":"ALL_CHECKS_PASSED","checks":[...]}},...}

# submit (or idempotently re-check) the selected action at the gate directly
curl -X POST http://localhost:8000/api/cases/1/gate/evaluate

# the timeline shows every gate decision, allowed or blocked
curl http://localhost:8000/api/cases/1
```

**Deliverable:** policy engine that returns ALLOW / BLOCK / ESCALATE with a machine-readable reason — done.

### Phase 8 — Recovery Action & Outcome Simulator (COMPLETE)

**Objective:** complete the action layer without falsely claiming that the system can force a real customer payment.

**Built:**
- **Action executor** (`backend/services/action_executor.py`, `execute_action`): executes only gate-ALLOWed actions. A case must be in `SAFETY_CHECK`, and the Phase 7 gate is **re-verified at execution time** — the world may have changed since the ALLOW. A re-verification failure is applied exactly like a gate verdict (expired window → `STOPPED`, captured payment → `ESCALATED`, …), logged as a `safety_gate` row flagged `recheck: true`; nothing executes. Execution itself is simulated end to end:
  - `RETRY_PAYMENT_LINK` → `create_recovery_payment` (authorized) creates the **simulated recovery payment/link**: a `PENDING` payment row on the same order (`pay_rec_{case}_{attempt}`, `rlink_{case}_{attempt}`) with `gateway_metadata` labelling it `simulated`, its recovery case and its link id — no real payment operation.
  - `RETRY_PAYMENT_LINK` and `SEND_NOTIFICATION_ONLY` → `send_recovery_notification` (authorized) records the **simulated notification** with its channel (`DEFAULT_NOTIFICATION_CHANNEL` = EMAIL, configurable) and a masked recipient — no real email/SMS/WhatsApp is ever delivered.
  - `WAIT_AND_MONITOR` → monitoring only: no payment, no notification, no attempt consumed.
  - Act actions increment `attempt_count`; the case moves `SAFETY_CHECK → ACTION_EXECUTED` (rule 8, audited `action.executed` with the payment id, link id, channel and `simulated: true`) `→ WAITING_FOR_RESULT` (rule 11, `action.monitoring_started`). A simulated execution failure escalates rather than half-executing.
  - **Idempotency:** each execution is keyed (`exec:{case}:{action}:{attempt}`) and recorded in `agent_actions` (`execute_recovery_action`); re-executing a waiting case replays the recorded execution without creating a new payment, notification or audit trail. The recovery payment handler is itself idempotent per case+attempt.
  - **Tool-layer authorization:** `execute_tool` now takes the `authorized` flag — set only by the executor after its gate re-verification. Any other act-tool call (the LLM's, a direct one) is blocked with `GATE_AUTHORIZATION_REQUIRED`, logged `allowed = false`, never executed. The check_recovery_result tool reports the recovery payment's current state.
- **Outcome simulator** (`backend/services/outcome_simulator.py`, `simulate_outcome`): injects the scripted outcome of an executed action — `SUCCESS` / `FAILED` / `STILL_PENDING` / `NO_RESPONSE` — through the **real event pipeline** (webhook replay, source `REPLAY`), so every world-state change carries the full Phase 3 idempotency, audit and risk-evaluation machinery. All outcomes are explicitly labelled `simulated` in the tool output, the audit trail and the API response:
  - `SUCCESS` → captures the simulated recovery payment (`payment.captured` replay; order → `PAID`). When no recovery payment exists (notification-only/monitor), it simulates the customer's independent retry as a **new captured payment on the same order** (`pay_retry_{case}_{attempt}`). The case **stays `WAITING_FOR_RESULT`** — revenue is never marked recovered because a notification was sent or a payment captured; verification and attribution (rule 12, Section 11) are Phase 9.
  - `FAILED` → fails the simulated recovery payment (`payment.failed` replay; the active case absorbs the duplicate failure per the Phase 4 order-level idempotency) and applies the retry cycle: attempts remaining and window open → `DIAGNOSING` (rule 13, `outcome.retry`) for a full new agent cycle; attempts exhausted → `NOT_RECOVERED` (rule 14, `outcome.not_recovered`).
  - `STILL_PENDING` / `NO_RESPONSE` → keep monitoring (rule 15, `outcome.still_pending` / `outcome.no_response`), no state change.
  - Outcome on an expired case → `STOPPED` (rule 16 at outcome time, `case.window_expired`); outcomes on non-waiting or terminal cases are structured NOOPs.
- **API:** `POST /api/cases/{id}/action/execute` and `POST /api/cases/{id}/outcome` (body `{"outcome": "SUCCESS"|"FAILED"|"STILL_PENDING"|"NO_RESPONSE"}`, optional `created_at` for deterministic replays; 404 unknown case, 422 invalid outcome).
- Full retry loop now works mechanically: `DETECTED → … → WAITING_FOR_RESULT` → FAILED → `DIAGNOSING` → agent re-run (score drops with the prior-attempt penalty) → gate → execute attempt 2 → FAILED → `NOT_RECOVERED`.

**Verified:**
- Pytest suite `backend/tests/test_action_outcome.py` — 14 tests against the real migrated schema: RETRY execution creates the simulated payment + notification (metadata, masking, `agent_actions` sequence, three-event audit tail), idempotent re-execution (single payment, single `execute_recovery_action` row, single `action.executed`), notification-only and monitor actions (no payment, attempt counting only for act actions), NOOP on non-`SAFETY_CHECK`/terminal cases, execution-time re-verification (expired window → `STOPPED`, captured payment → `ESCALATED`, both with `recheck` gate rows and nothing executed), unauthorized act-tool calls still blocked (`GATE_AUTHORIZATION_REQUIRED`), `check_recovery_result` reporting the recovery payment, SUCCESS capturing the recovery payment (order `PAID`, case still waiting, repeated SUCCESS is a NOOP), SUCCESS on notification-only simulating the independent retry payment, the full FAILED retry cycle (attempt 1 → `DIAGNOSING` → re-run → attempt 2 → `NOT_RECOVERED`), STILL_PENDING/NO_RESPONSE keeping monitoring, outcome on an expired case stopping it, and both API endpoints (404/422/200, replay, timeline).
- `backend/tests/test_agent.py` act-tool test updated for the executor-era block reason (suite total 77, all passing).
- Live smoke test on the development database (server via `uvicorn main:app`): seed payment `pay_0077` → agent run (gate ALLOW) → execute → **`WAITING_FOR_RESULT`** with `pay_rec_1_1` / `rlink_1_1`, EMAIL notification, attempt 1, everything labelled simulated; re-execute returned the idempotent `replay: true`; outcome SUCCESS captured the recovery payment (`CAPTURED`, order `PAID`) while the case correctly stayed `WAITING_FOR_RESULT` pending Phase 9 verification. Seed payment `pay_0186` → full two-attempt cycle: execute (cautious notification-only) → outcome FAILED → `DIAGNOSING` → agent re-run (score 74 → 72 with the prior-attempt penalty, gate ALLOW) → execute attempt 2 → outcome FAILED → **`NOT_RECOVERED`** with the complete two-cycle audit timeline (`2 × diagnosis/scored/action_selected/gate/executed` + `outcome.retry` + `outcome.not_recovered`), and no duplicate cases created. The database was reset to the pristine seeded state afterward.

**How to run the tests** (from `backend/`):

```bash
.venv\Scripts\python -m pytest
```

**How to run the action loop** (server running via `uvicorn main:app`):

```bash
# 1. create a case and run the agent to a gate ALLOW
curl -X POST http://localhost:8000/api/events/synthetic -H "Content-Type: application/json" \
  -d '{"payment_id":"pay_0077","event":"payment.failed","error_description":"Insufficient funds"}'
curl -X POST http://localhost:8000/api/cases/1/agent/run

# 2. execute the ALLOWed action (simulated; idempotent)
curl -X POST http://localhost:8000/api/cases/1/action/execute
# → {"decision":"WAITING_FOR_RESULT","execution":{"recovery_payment_id":"pay_rec_1_1","recovery_link_id":"rlink_1_1",...,"simulated":true}}

# 3. script the outcome (simulated, via event replay)
curl -X POST http://localhost:8000/api/cases/1/outcome -H "Content-Type: application/json" \
  -d '{"outcome":"SUCCESS"}'
# → the recovery payment is CAPTURED, the order PAID; the case waits for Phase 9 verification

# 4. inspect the full timeline
curl http://localhost:8000/api/cases/1
```

**Deliverable:** safe simulated recovery action → scripted outcome → verification flow — done (verification and attribution themselves are Phase 9).

### Phase 9 — Verification & Attribution (COMPLETE)

**Objective:** close the loop correctly and distinguish a real recovery from unrelated activity.

**Built:**
- **Verification & attribution service** (`backend/services/verification.py`, `verify_outcome`): the explicit step after an executed action's outcome. It reads the latest payment/order state and applies the Section 11 attribution rule deterministically. Verification runs only on `WAITING_FOR_RESULT` cases (terminal and non-waiting cases are structured NOOPs) and evaluates every successful payment on the case's order against the four attribution checks (associated / after approved action / within case window / never credited twice):
  - **Attributable success → rule 12:** `WAITING_FOR_RESULT → RECOVERED`; the credited payment, amount and timestamp are stored on the case (`recovered_payment_id`, `recovered_amount`, `recovered_at`) and the transition is audited as `verification.recovered` with the complete evidence — payment id, amount, capture time and its basis, event source (`REPLAY` / `SYNTHETIC` / `RAZORPAY_WEBHOOK`), simulated flag, action execution time, case expiry and the full check list. Revenue is credited only here — never on execution or capture alone.
  - **Verified success that fails attribution → rule 18:** `NOT_RECOVERED` (`verification.not_recovered`) with per-candidate machine-readable rejection reasons (`CAPTURED_BEFORE_APPROVED_ACTION` / `OUTSIDE_CASE_WINDOW` / `ALREADY_CREDITED_TO_ANOTHER_CASE`) — an independent customer retry outside the attributable action window is never credited.
  - **No success yet:** the case keeps monitoring (rule 15) with a side-effect-free re-check; **expired window at verification → rule 20:** `STOPPED` (`case.window_expired`, `detected_by: verification`).
  - **Ambiguity escalates (rule 17):** missing order identity, no approved action recorded for a waiting case, or an order that is `PAID` with no capturable payment → `ESCALATED` (`verification.escalated`) rather than guessing.
  - **Idempotent by construction:** a `RECOVERED` case is terminal (re-verify returns the recorded recovery as a NOOP), and monitoring re-checks change nothing. Every verification run on a waiting case is logged to `agent_actions` (tool `verify_outcome`, ungated system step).
- **Attribution storage** (migration `0003_attribution`): `recovery_cases.recovered_payment_id` / `recovered_amount` / `recovered_at`, with the unique index `uq_recovery_cases_recovered_payment` enforcing rule 19 at the schema level — the same successful payment can never be credited to two cases, in the same spirit as the Phase 2 invariants.
- **Metrics service** (`backend/services/metrics.py`, `GET /api/metrics`): all Section 11 metrics computed from stored data only — revenue at risk (total / eligible / escalated-excluded, with the documented eligibility decision), recovered revenue and recovered case count, recovery rate, attempts and successful recoveries, average recovery time (mean `recovered_at − created_at` over `RECOVERED` cases), escalation rate, and invalid/blocked actions (all `agent_actions` with `allowed = false`, broken down by tool).
- **APIs & tools:** `POST /api/cases/{id}/verify` (404 unknown case, 200 with the verification result and full attribution evidence); the case list/detail now expose `recovered_payment_id` / `recovered_amount` / `recovered_at`; the `check_recovery_result` tool reports the attribution state (`case_recovered`, `recovered_payment_id`, `recovered_amount`, `recovered_at`). The Phase 8 executor and outcome simulator are unchanged in behaviour — a SUCCESS outcome still only changes world state; crediting happens exclusively in verification.

**Verified:**
- Pytest suite `backend/tests/test_verification_metrics.py` — 15 tests against the real migrated schema (suite total 92, all passing):
  1. happy-path attribution: RETRY execution → SUCCESS outcome → verify → `RECOVERED` with all four checks passing, `pay_rec_{case}_1` credited, the `verification.recovered` audit transition with rule 12 and the evidence payload, and the `verify_outcome` agent-action row
  2. verification is final: re-verify of a `RECOVERED` case is a NOOP that returns the recorded recovery — no second credit, no second audit transition
  3. notification-only success attributed to the simulated independent customer retry (`pay_retry_{case}_1`)
  4. monitor-action success within the window attributed (`pay_retry_{case}_0`, no attempt consumed)
  5. verification without a success keeps monitoring (rule 15, logged, side-effect-free, repeatable)
  6. verification of an expired waiting case stops it (rule 20, `case.window_expired` with `detected_by: verification`)
  7. a verified success that predates the approved action is **not** credited (rule 18, rejection reason `CAPTURED_BEFORE_APPROVED_ACTION`, `NOT_RECOVERED`)
  8. NOOPs on non-waiting (`DETECTED`, `SAFETY_CHECK`) and terminal cases
  9. the same payment is never credited twice: the service guard rejects an already-credited payment (`ALREADY_CREDITED_TO_ANOTHER_CASE`), the schema unique index rejects the duplicate (`IntegrityError`), and the metrics count the payment exactly once
  10. verification-time escalation on conflicting order state (`PAID` order, no capturable payment — rule 17)
  11. verification-time escalation on missing order identity (rule 17)
  12. `check_recovery_result` reports the attribution state before and after recovery
  13. full metrics computation from a four-case scenario (recovered ₹2,000 / two-attempt `NOT_RECOVERED` ₹3,000 / born-escalated ₹1,500 / waiting ₹1,000): total ₹7,500, eligible ₹6,000, recovered ₹2,000, recovery rate 1/3, 4 attempts, 1 success, average recovery time > 0, escalation rate 0.25, blocked actions broken down by tool
  14. metrics on an empty database (zeros and nulls, no crash)
  15. both endpoints: `POST /verify` 404/200/NOOP, the case detail timeline ending `outcome.success → verification.recovered`, and `GET /api/metrics`
- Migration `0003_attribution` upgrade → downgrade → re-upgrade verified on the development database.
- Live smoke test on the development database (server via `uvicorn main:app`): seed payment `pay_0077` → agent run (score 96, HIGH, gate ALLOW) → execute → **verify before any success correctly returned `NO_SUCCESS_YET`** → outcome SUCCESS → verify → **`RECOVERED` ₹257.00 attributed to `pay_rec_1_1`** (all four checks true, event source `REPLAY`, simulated labelled) → re-verify returned the idempotent NOOP with the recorded recovery. Seed payment `pay_0186` → cautious `SEND_NOTIFICATION_ONLY` → outcome SUCCESS (independent retry `pay_retry_2_1`) → verify → **`RECOVERED` ₹13,457.00**. `GET /api/metrics`: 2 cases, ₹13,714.00 revenue at risk and recovered, recovery rate 1.0, 2 attempts / 2 successful recoveries, average recovery time ≈ 24.5 s, escalation rate 0.0, blocked actions 0. The database was reset to the pristine seeded state afterward.

**How to run the tests** (from `backend/`):

```bash
.venv\Scripts\python -m pytest
```

**How to verify and attribute** (server running via `uvicorn main:app`):

```bash
# 1. create a case, run the agent to a gate ALLOW, execute the action, script an outcome
curl -X POST http://localhost:8000/api/events/synthetic -H "Content-Type: application/json" \
  -d '{"payment_id":"pay_0077","event":"payment.failed","error_description":"Insufficient funds"}'
curl -X POST http://localhost:8000/api/cases/1/agent/run
curl -X POST http://localhost:8000/api/cases/1/action/execute
curl -X POST http://localhost:8000/api/cases/1/outcome -H "Content-Type: application/json" \
  -d '{"outcome":"SUCCESS"}'

# 2. verify the outcome and attribute the revenue (Phase 9; idempotent)
curl -X POST http://localhost:8000/api/cases/1/verify
# → {"decision":"RECOVERED","verification":{"result":"RECOVERED","attribution":{...},"recovery":{...}}}

# 3. the recovery metrics, computed from stored data only
curl http://localhost:8000/api/metrics
# → {"cases":{...},"revenue_at_risk":{"total":"257.00","eligible":"257.00",...},"recovered":{"revenue":"257.00","cases":1},"recovery_rate":1.0,...}
```

**Deliverable:** verified terminal outcomes, a deterministic attribution rule that never credits unattributable or duplicate successes, and mathematically consistent recovery metrics — done.

### Phase 10 — Dashboard, Metrics & Audit Timeline (COMPLETE)

**Objective:** make the agent's value visible to a merchant and to buildathon judges.

**Built:**
- **Next.js (App Router) + Tailwind CSS frontend** (`frontend/`, Node 26 / Next 16 / React 19 / Tailwind v4): server-rendered pages fetch live data from the existing Phase 9 APIs (`GET /api/metrics`, `GET /api/cases`, `GET /api/cases/{id}`) with no caching; browser `/api/*` requests are proxied to the FastAPI backend via Next.js rewrites (`BACKEND_URL`, default `http://localhost:8000`). **No backend code was modified for Phase 10.**
- **Dashboard (`/`)**: the four required cards — Revenue at Risk (eligible, with the total and escalated-excluded breakdown), Recovered Revenue (amount + recovered case count), Recovery Rate and Active Cases — plus the operations metrics (attempts, successful recoveries, average recovery time, escalation rate, invalid/blocked actions with per-tool breakdown), a case-status distribution bar chart and the ten most recent cases.
- **Case list (`/cases`)**: every required column — amount at risk, payment method, failure reason, score (with the HIGH/MEDIUM/LOW band badge), current status, attempts — plus the 24-hour **deadline** (time remaining, "closed" on terminal cases, "expired" once past), the recovered amount, status-filter chips for all 11 states and pagination.
- **Case detail (`/cases/{id}`)**: summary tiles (revenue at risk, recovered, score, attempts, deadline with the absolute expiry), the verified-recovery **attribution card** (credited payment, amount, captured time, simulated label, pointer to the evidence), payment/order/customer context, and the **AI timeline** — a merged chronological view of every audit-log transition and every agent tool call from detection to verified recovery: human-readable labels, state-transition chips, ALLOWED / BLOCKED / READ verdict chips, "why" lines (score + band, selected action + reason, gate verdict + reason + safe alternative, outcome notes, attribution evidence), simulated tags wherever the stored payload says `simulated: true`, and expandable raw payload / tool-output JSON for every entry. Unknown case ids render a not-found page.
- **Pipeline controls** on the case page — run agent / execute action / simulate outcome / verify outcome — call the existing Phase 5–9 endpoints through the proxy and refresh the live view; each control is enabled only for the statuses where the backend accepts it, and outcome injection is explicitly labelled simulated.
- **Zero fake data**: every displayed number is computed by the backend from stored data. With the backend unreachable the pages show an explicit error state (no cached or invented values); an empty database shows the empty state with a hint on how the first case is created. A persistent banner labels demo mode, and simulated tags appear wherever stored payloads mark simulation.

**Verified:**
- Frontend: `npm test` — **14 vitest unit tests** for the display helpers (INR formatting with Indian grouping, percent, duration humanization, deadline countdown, UTC timestamps, score-band thresholds); `npm run lint` — clean; `npm run build` — production build succeeds with every route server-rendered on demand.
- Backend: unchanged — **92 pytest tests passing**, no Phase 1–9 code modified.
- **End-to-end** (backend via `uvicorn`, frontend via `next start`, on the seeded database): the empty-state dashboard rendered correctly with zero cases; a full case was driven **through the Next.js proxy** (synthetic failed event → agent run → execute → SUCCESS outcome → verify) and the pages were asserted to contain the real stored data — dashboard: ₹13,714.00 eligible revenue at risk, ₹257.00 recovered, 1.9% recovery rate, average recovery time, escalation rate 0.0, 0 blocked actions; case list: both cases with method, failure reason `INSUFFICIENT_FUNDS`, score badges, deadlines and pagination; case #1 detail: the attribution card (`pay_rec_1_1`, ₹257.00) and the complete timeline (case created → agent diagnosis → score → action selected → gate ALLOW → action executed → outcome success → recovery verified & attributed) with tool-call rows, allowed/read verdict chips, simulated tags and expandable payloads; case #2 (notification-only, waiting): a live "23h 58m left" deadline with the outcome/verify controls enabled; the status filter (`?status=RECOVERED`) returned exactly the recovered case; `/cases/999` returned the 404 not-found page. The database was reset to the pristine seeded state afterward.

**How to run** (backend from `backend/`, frontend from `frontend/`):

```bash
# 1. backend
.venv\Scripts\python -m uvicorn main:app                  # http://localhost:8000

# 2. frontend
npm install
npm run dev                                                # http://localhost:3000
# or production
npm run build && npm run start

# 3. create a case and drive the loop from the case page (or via curl as in Phases 3-9)
curl -X POST http://localhost:8000/api/events/synthetic -H "Content-Type: application/json" \
  -d '{"payment_id":"pay_0077","event":"payment.failed","error_description":"Insufficient funds"}'
# → open http://localhost:3000/cases/1 and use: Run agent → Execute action → Simulate outcome → Verify outcome

# frontend tests
npm test && npm run lint
```

**Deliverable:** merchant-facing dashboard + explainable audit trail — done.

### Phase 11 — Async Processing & Queue Decision (COMPLETE)

**Objective:** add background processing only after the core synchronous workflow is working.

**Built:**
- **Durable job abstraction** (`backend/services/jobs.py`, migration `0004_background_jobs`, table `background_jobs`): jobs are rows — `job_key` (unique, deterministic — scheduling is idempotent exactly like the gate/executor keys of Phases 7–8), `name` (a handler in the registry), `params`, `due_at`, `status` (PENDING / DONE / FAILED), `recurring_interval_seconds`, `result`, `error`. Handlers call the existing services only: `run_agent`, `verify_outcome`, `simulate_outcome`, `expiry_sweep`. Each job executes in its own transaction — a failure is recorded on the row (`UNKNOWN_JOB` or the handler error) and never blocks other jobs. Jobs are scheduled **transactionally with the pipeline step that needs them**, so a crash never loses work. This registry + row store is the seam where Redis + Celery later replaces the runner (a worker draining the same rows) **without touching the workflow**.
- **In-process scheduler** (`backend/services/scheduler.py`): one lightweight asyncio loop started with the FastAPI app (lifespan, `SCHEDULER_ENABLED`, tick `SCHEDULER_INTERVAL_SECONDS` = 10 s). Each tick seeds the recurring jobs and executes everything due via the same `run_due_jobs` used by the synchronous API. **Queue decision: Redis + Celery is NOT introduced** — the single-instance asyncio runner over the durable table is sufficient for the simulated, single-merchant workload; the abstraction above keeps the upgrade to a real broker a runner swap, and retry/backoff policies become Celery's job if asynchronous reliability is ever genuinely required.
- **Autonomous loop hooks** (all additive, no Phase 1–10 behaviour changed):
  - **case creation → agent run** (the Phase 5 promise): a cleanly created case schedules `agent:{case}:0` (due immediately); born-escalated cases never schedule an agent.
  - **execution → delayed verification**: a successful action execution schedules `verify:{case}:{attempt}:executed` at now + `VERIFICATION_DELAY_SECONDS` (30 s) — the delayed status check / monitoring job.
  - **SUCCESS outcome → verification**: the simulator schedules `verify:{case}:{attempt}:outcome` due immediately, so a captured recovery is verified and credited without any manual call.
  - **FAILED outcome (retry-eligible) → next agent run**: schedules `agent:{case}:{attempt}` so the retry cycle continues autonomously.
  - **scheduled simulated outcomes**: `POST /api/cases/{id}/outcome` accepts `delay_seconds` to schedule the scripted outcome as a job instead of running it now.
  - **general expiry sweep** (rule 16, deferred from Section 8 until now): the recurring `sweep:expiry` job (`SWEEP_INTERVAL_SECONDS` = 60 s) stops every **active** case whose 24-hour window expired — from any active state, through the audited state machine (`case.window_expired`, `detected_by: expiry_sweep`); terminal cases are never touched.
- **APIs** (`backend/api/jobs.py`): `GET /api/jobs` (transparency — every job, its status, result and due time; filter by status) and `POST /api/jobs/run` (execute due jobs now; `force: true` runs every PENDING job — the deterministic trigger used by tests and the demo). `GET /health` reports `scheduler_enabled`.

**Verified:**
- Pytest suite `backend/tests/test_jobs.py` — **17 tests** against the real migrated schema (suite total 109, all passing; scheduler disabled in the test process via `SCHEDULER_ENABLED=false`, exercised deterministically through `run_due_jobs` / the APIs):
  1. case creation schedules the agent job; born-escalated cases schedule nothing
  2. the scheduled agent job executes through the registry and drives the case to `SAFETY_CHECK` with the full audit timeline
  3. execution schedules the delayed verification job (future `due_at`, not run before due, executed with force, `NO_SUCCESS_YET` recorded, never re-run — idempotent by status)
  4. a SUCCESS outcome schedules verification and the case **recovers autonomously** through the job (`RECOVERED`, `pay_rec_{case}_1`, ₹2,000.00)
  5. a retry-eligible FAILED outcome schedules the next agent run, which re-selects a safe action autonomously
  6. the full autonomous retry cycle: agent → execute → FAILED → agent re-run → execute → FAILED → `NOT_RECOVERED` (no job scheduled past the attempt limit)
  7. the expiry sweep stops an expired waiting case (audited `case.window_expired`, `detected_by: expiry_sweep`) and a second sweep is a no-op
  8. the sweep stops expired cases from any active state (`DETECTED`) and never touches terminal cases
  9. the recurring sweep job seeds once (idempotent), reschedules itself by interval, and stays a single row
  10. `schedule_job` is idempotent by key; unknown job names FAIL without blocking other jobs; jobs not yet due are skipped
  11. `scheduler_tick` (the exact function the loop runs) seeds the recurring jobs and executes due jobs
  12. the jobs APIs: list (empty/after scheduling/422 on a bad status filter) and `POST /api/jobs/run` with force
  13. the scheduled-outcome endpoint: schedules idempotently, is skipped before due, executes with force, the captured payment exists, and the follow-up verification job recovers the case
- Migration `0004_background_jobs` upgrade → downgrade → re-upgrade verified on the development database.
- **Live smoke test on the development database with the real scheduler running** (tick 10 s): a failed event created case #1 → **within one tick the scheduler autonomously ran the agent** (score 96, `RETRY_PAYMENT_LINK`, gate ALLOW → `SAFETY_CHECK`, job `agent:1:0` DONE) → manual execution (the deliberate intervention point) → outcome `SUCCESS` scheduled with `delay_seconds=15` → the scheduler executed it, then the auto-scheduled verification job → **`RECOVERED` ₹257.00 via `pay_rec_1_1` with zero manual verification calls** (the later +30 s monitoring job ran as the correct terminal NOOP). A second failed event for the already-recovered payment correctly produced a born-escalated case (`AMBIGUOUS_CONFLICTING_STATE` — order already PAID, no agent job). The recurring sweep autonomously stopped a backdated expired case (`SAFETY_CHECK → STOPPED`, `detected_by: expiry_sweep`). Final metrics: 1 RECOVERED / 1 ESCALATED / 1 STOPPED. `GET /health` reported `scheduler_enabled: true`. The database was reset to the pristine seeded state afterward (background_jobs truncated with the rest).

**How to run** (from `backend/`; the scheduler starts automatically with the app):

```bash
.venv\Scripts\python -m uvicorn main:app          # scheduler runs every 10 s (SCHEDULER_ENABLED=false disables)

# watch the jobs
curl http://localhost:8000/api/jobs
# run everything pending right now (deterministic trigger)
curl -X POST http://localhost:8000/api/jobs/run -H "Content-Type: application/json" -d '{"force": true}'

# fully autonomous demo loop
curl -X POST http://localhost:8000/api/events/synthetic -H "Content-Type: application/json" \
  -d '{"payment_id":"pay_0077","event":"payment.failed","error_description":"Insufficient funds"}'
#   → the scheduler runs the agent within one tick (case -> SAFETY_CHECK)
curl -X POST http://localhost:8000/api/cases/1/action/execute
curl -X POST http://localhost:8000/api/cases/1/outcome -H "Content-Type: application/json" \
  -d '{"outcome":"SUCCESS","delay_seconds":15}'
#   → the scheduler executes the outcome, then verification: RECOVERED, no manual verify
```

**Deliverable:** minimal asynchronous verification mechanism with a clear upgrade path — done.

### Phase 12 — Testing, Demo & Definition of Done (COMPLETE)

**Objective:** validate the full loop under normal, blocked and ambiguous scenarios.

**Built & verified:**
- **Scenario validation suite** (`backend/tests/test_phase12_scenarios.py` — 13 tests, each driving the real pipeline end to end and asserting the final state plus audit evidence):
  1. **successful recovery**: detected → diagnosed → scored → action selected → gate ALLOW → executed → SUCCESS → verified `RECOVERED` with the attributed payment (`pay_rec_{case}_1`, ₹2,000.00), the complete audit chain in order, and the attribution evidence on the `verification.recovered` entry
  2. **failed recovery after retry limit**: FAILED → autonomous retry (agent re-run through the job layer) → FAILED again → `NOT_RECOVERED` with exactly 2 executed attempts and no agent job past the attempt limit
  3. **pending payment that becomes successful before recovery action**: a PENDING payment routes to verification (no case); its capture is `ALREADY_SUCCESSFUL_IGNORED` — verification-first, the agent is never invoked
  4. **customer pays before execution**: a new captured payment on the order after case creation → the gate's execution-time re-check escalates (`CONFLICTING_ORDER_STATE`), nothing executes, no recovery payment exists, attempt count 0
  5. **duplicate webhook/event**: redelivery of the same event returns `DUPLICATE`, exactly one stored event, one case, one `risk.case_created` audit — zero double effects
  6. **amount mismatch**: gateway amount ≠ authoritative order amount → born `ESCALATED` (`AMBIGUOUS_AMOUNT_MISMATCH`), no action, no agent job
  7. **conflicting order state**: order already `PAID` while the payment fails → born `ESCALATED` (`AMBIGUOUS_CONFLICTING_STATE`)
  8. **repeated uncertain failures**: repeated NETWORK_ERROR/BANK_TIMEOUT failures on one order → escalation, and even after draining every scheduled job no ambiguous case ever reaches a recovery action (no simulated payment exists)
  9. **duplicate active case on an order is blocked**: a second failure on an order with an active case returns `DUPLICATE_ACTIVE_CASE` (one case total)
  10. **duplicate recovery action execution is blocked**: re-executing a waiting case replays idempotently — same idempotency key, exactly one recovery payment, one `action.executed` audit, attempt count unchanged
  11. **unauthorized action tool call is blocked**: calling `create_recovery_payment` outside the executor returns `BLOCKED` / `GATE_AUTHORIZATION_REQUIRED`, nothing is created
  12. **expired window stops the case at the gate**: the 24-hour window expires before execution → `STOPPED` (`gate.case_stopped`), nothing executed
  13. **the definition of done in one test**: case creation → scheduled agent run → one deliberate execution → scripted outcome → the job layer completes verification — `RECOVERED` with zero manual pipeline calls
- **Synthetic evaluation** (`backend/evaluation.py`, `python -m evaluation`): resets to the pristine seed (seed = 42), injects a failure event for **every** failed seed payment, lets the Phase 11 job layer run the agent for every case, then drives the loop — execute (the deliberate intervention) → deterministically scripted simulated outcomes (seeded RNG: 60% success for act actions, 30% for monitoring) → autonomous verification — until convergence, and computes the final metrics from stored data only. Fully deterministic: two consecutive runs produce identical results.
- **Final metrics of the synthetic evaluation run** (121 failure events → 121 recovery cases; 100 clean + 21 born-ambiguous exactly as the seed designs; 8 additional cases escalated by the agent's own ESCALATE recommendation; the 35 outcome-replay failure events were all correctly duplicate-suppressed — no spurious cases):

  | Metric | Value |
  |---|---|
  | Cases | 121 total — **77 RECOVERED, 15 NOT_RECOVERED, 29 ESCALATED, 0 active** |
  | Revenue at risk | ₹645,551.00 total — ₹459,497.00 eligible (₹186,054.00 escalated excluded) |
  | Recovered revenue | **₹400,459.00** across 77 cases |
  | Recovery rate | **87.2%** of eligible revenue at risk |
  | Attempts | 127 executions (92 first attempts + 35 autonomous retries), 77 successful recoveries |
  | Average recovery time | ≈ 8.5 s (measured on the simulated run timeline: case creation → verified recovery) |
  | Escalation rate | 24.0% (21 born-ambiguous per the seed's designed ambiguity + 8 agent-recommended) |
  | Invalid / blocked actions | 0 (the gate ALLOWed every executed action; no unauthorized tool call occurred) |

- **Stable 5-minute demo** (validated live end to end — backend + production frontend, every number below observed in the rendered pages; no video/media produced, the script is the demo):
  1. `backend/`: `.venv\Scripts\python -m uvicorn main:app` — `frontend/`: `npm run build && npm run start` → open **http://localhost:3000** — the dashboard shows the empty state: ₹0.00 at risk, ₹0.00 recovered, 0 active cases (real zeros, nothing faked).
  2. Create the primary case **deterministically**: `curl -X POST http://localhost:8000/api/demo/case` — this selects the next failed seed payment that is guaranteed (given the current database state) to produce a fresh, clean case, posts its failure event through the real intake pipeline, runs the scheduled agent job and returns the case in `SAFETY_CHECK` (the executable state). Repeatable **without a database reset**: every call consumes a different payment, so a demo can be run again immediately after a completed one — fixed demo payment ids are no longer needed. (A raw `POST /api/events/synthetic` for a specific payment still works on a pristine database.)
  3. Within one scheduler tick (10 s) the agent **runs autonomously** (Phase 11): open `/cases/{id}` — the AI timeline shows diagnosis, the deterministic score and band, the selected action with its reason, and the safety gate's **ALLOW** with all eight checks.
  4. Press **Execute action** (the single deliberate intervention): the timeline shows the simulated recovery payment `pay_rec_{case}_1`, the link and the EMAIL notification — all labelled *simulated*.
  5. Press **Simulate outcome** (SUCCESS): the scripted outcome is scheduled; the scheduler executes it and then the auto-scheduled verification job — **no manual verify**.
  6. Watch the case flip to **RECOVERED** with the attribution card (the credited payment, credited only after verification), and the dashboard update — recovered revenue, recovery rate, recovered cases, average recovery time — every number from stored data.

**How to run** (from `backend/`):

```bash
.venv\Scripts\python -m pytest                          # full suite: 129 tests (13 Phase 12 scenarios + 7 demo-case)
.venv\Scripts\python -m evaluation                      # synthetic evaluation + final metrics (~30 s)
.venv\Scripts\python -m evaluation --no-reset           # evaluate the current DB state
curl -X POST http://localhost:8000/api/demo/case        # deterministic executable demo case (repeatable, no reset needed)
```

**Deliverable:** validated end-to-end loop + measured metrics + stable 5-minute demo — done.

---

## 13. Definition of Done

- Working dashboard
- Event intake (webhook + synthetic events)
- Revenue-at-risk detection
- AI tool calling (GLM 5.3)
- Safety layer with stopping rules
- One complete verified recovery workflow
- Audit trail
- Synthetic evaluation
- Measured metrics

---

## 14. Build Status

**Current phase: Phase 12 — COMPLETE. All 12 phases of the blueprint are done.**

Phase 12 deliverables:
- Scenario validation suite (`tests/test_phase12_scenarios.py`, 13 tests): every scenario the blueprint lists — successful recovery, failed recovery after retry limit, pending-becomes-successful before the action, duplicate webhook/event, amount mismatch, ambiguous escalations (conflicting state, repeated uncertain failures), duplicate active case on an order, duplicate/idempotent execution, unauthorized tool-call blocking, window expiry at the gate, and the one-test definition of done (full autonomous loop) — driven through the real pipeline with final-state and audit-evidence assertions
- Synthetic evaluation harness (`evaluation.py`, `python -m evaluation`): the complete seed dataset through the system — failure events for all 121 failed payments, agent runs via the Phase 11 job layer, deliberate executions, deterministically scripted simulated outcomes, autonomous verification — converging to final metrics computed from stored data only; two consecutive runs produce identical results
- Final measured metrics: 121 cases → **77 RECOVERED / 15 NOT_RECOVERED / 29 ESCALATED / 0 active**; ₹645,551.00 revenue at risk (₹459,497.00 eligible); **₹400,459.00 recovered revenue; 87.2% recovery rate**; 127 attempts (92 + 35 autonomous retries); escalation rate 24.0% (21 born-ambiguous by seed design + 8 agent-recommended); 0 invalid/blocked actions; the 35 outcome-replay events all duplicate-suppressed
- Stable 5-minute demo script (validated live end to end, no video/media produced): empty dashboard → deterministic demo case (`POST /api/demo/case`) → autonomous agent within one tick → timeline with score and band and gate ALLOW → deliberate execute → scheduled simulated SUCCESS → autonomous verification → RECOVERED and the dashboard updating with the recovered revenue and recovery rate
- Full verification: backend **122 tests passing** (13 new), frontend 14 vitest tests + clean eslint + production build; the development database reset to the pristine seeded state afterward

**Post-Phase-12 fix — deterministic demo-case reliability (COMPLETE):** the demo previously relied on fixed seed payment ids, so a second demo run without a database reset hit leftover terminal state (PAID orders, escalated cases) and `/action/execute` correctly NOOP'd. Added `services/demo.py` + `POST /api/demo/case`: selects the next failed payment that is guaranteed — from the current database state — to produce a fresh, clean case (order exists and not PAID, amounts match, non-uncertain failure, no prior case on the order), posts its failure event through the real intake pipeline, runs the scheduled agent job and returns the first case reaching `SAFETY_CHECK`, recording skipped candidates (e.g. agent-escalated RISK_BLOCKED) in `attempted`. No validation, state-machine rule, idempotency, authorization or terminal-state protection was weakened — selection only picks inputs the existing rules already accept. Verified: 7 new tests (`tests/test_demo_case.py`, suite total 129) including the full workflow with every persisted artefact asserted (case fields, the complete audit chain, agent actions with allow verdicts, background job rows, payment events, metrics); live end-to-end — four consecutive demo calls without a reset each produced an executable case, two driven to RECOVERED (₹12,385 via an independent retry and ₹257 via the recovery payment) with autonomous verification; the database was reset afterward.

**Definition of Done (Section 13) — satisfied:** working dashboard ✓ · event intake (webhook + synthetic) ✓ · revenue-at-risk detection ✓ · AI tool calling (GLM 5.3, with deterministic offline fallback) ✓ · safety layer with stopping rules ✓ · one complete verified recovery workflow ✓ · audit trail ✓ · synthetic evaluation ✓ · measured metrics ✓.

The database holds the deterministic seed data and no events, cases or jobs — the exact pristine state from which the evaluation (`python -m evaluation`) or the 5-minute demo reproduces every number in this README deterministically.
