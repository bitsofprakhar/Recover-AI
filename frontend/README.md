# RecoverAI Frontend — Merchant Dashboard

Next.js (App Router) + Tailwind CSS dashboard for the RecoverAI revenue recovery agent (Phase 10).

- **Dashboard** (`/`): Revenue at Risk, Recovered Revenue, Recovery Rate and Active Cases cards, operations metrics (attempts, successful recoveries, average recovery time, escalation rate, invalid/blocked actions), case status distribution and recent cases.
- **Cases** (`/cases`): case list with amount at risk, payment method, failure reason, score, current status, attempts and the 24-hour deadline, with status filter and pagination.
- **Case detail** (`/cases/{id}`): case summary, attribution result, payment/order/customer context, pipeline controls (run agent / execute action / simulate outcome / verify outcome) and the full AI timeline — every audit transition and tool call, including why an action was chosen and why anything was blocked.

Every displayed number is fetched from the backend APIs (`GET /api/metrics`, `GET /api/cases`, `GET /api/cases/{id}`) and computed from stored data only; nothing is faked. Simulated actions and outcomes are labelled as simulated throughout.

## Run

```bash
# 1. backend first (from ../backend)
.venv\Scripts\python -m uvicorn main:app          # http://localhost:8000

# 2. frontend
npm install
npm run dev                                        # http://localhost:3000
# or production
npm run build && npm run start
```

The backend base URL can be overridden with `BACKEND_URL` (default `http://localhost:8000`); `/api/*` requests from the browser are proxied to it via Next.js rewrites.

## Tests

```bash
npm test                                           # vitest unit tests (display helpers)
npm run lint                                       # eslint
```
