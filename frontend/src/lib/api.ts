/** Server-side data access for the RecoverAI dashboard.
 *
 * Every number shown in the UI comes from the backend APIs (`GET /api/metrics`,
 * `GET /api/cases`, `GET /api/cases/{id}`), which compute everything from
 * stored data only. Nothing is faked or hardcoded here. The backend base URL
 * is configurable via BACKEND_URL (default http://localhost:8000).
 */
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function fetchJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}${path}`, { cache: "no-store" });
  } catch (err) {
    throw new ApiError(0, `backend unreachable at ${BACKEND_URL} (${String(err)})`);
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // keep HTTP status detail
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export interface CasePayment {
  payment_id: string;
  status: string;
  amount: string;
  method: string;
  failure_reason: string | null;
}

export interface CaseOrder {
  order_id: string;
  status: string;
  amount: string;
  currency: string;
}

export interface CaseCustomer {
  customer_id: string;
  name: string;
  email: string;
  phone: string;
}

export interface CaseSummary {
  id: number;
  status: string;
  revenue_at_risk: string;
  diagnosis: unknown;
  score: number | null;
  selected_action: string | null;
  attempt_count: number;
  expiry: string;
  recovered_payment_id: string | null;
  recovered_amount: string | null;
  recovered_at: string | null;
  created_at: string;
  updated_at: string;
  payment: CasePayment | null;
  order: CaseOrder | null;
  customer: CaseCustomer | null;
}

export interface CaseListResponse {
  total: number;
  items: CaseSummary[];
}

export interface AgentActionRow {
  id: number;
  tool_name: string;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  allowed: boolean | null;
  created_at: string;
}

export interface AuditLogRow {
  id: number;
  event_type: string;
  from_status: string | null;
  to_status: string | null;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface CaseDetail extends CaseSummary {
  agent_actions: AgentActionRow[];
  audit_logs: AuditLogRow[];
}

export interface Metrics {
  generated_at: string;
  cases: { total: number; active: number; by_status: Record<string, number> };
  revenue_at_risk: {
    total: string;
    eligible: string;
    escalated_excluded: string;
    note: string;
  };
  recovered: { revenue: string; cases: number };
  recovery_rate: number | null;
  attempts: { total: number; successful_recoveries: number };
  average_recovery_time: { seconds: number; cases_counted: number } | null;
  escalation_rate: number | null;
  invalid_or_blocked_actions: { total: number; by_tool: Record<string, number> };
}

export const CASE_STATUSES = [
  "DETECTED",
  "DIAGNOSING",
  "SCORED",
  "ACTION_SELECTED",
  "SAFETY_CHECK",
  "ACTION_EXECUTED",
  "WAITING_FOR_RESULT",
  "RECOVERED",
  "NOT_RECOVERED",
  "STOPPED",
  "ESCALATED",
] as const;

export function getMetrics(): Promise<Metrics> {
  return fetchJson<Metrics>("/api/metrics");
}

export function getCases(opts: { status?: string; limit?: number; offset?: number } = {}): Promise<CaseListResponse> {
  const params = new URLSearchParams();
  if (opts.status && CASE_STATUSES.includes(opts.status as (typeof CASE_STATUSES)[number])) {
    params.set("status", opts.status);
  }
  params.set("limit", String(opts.limit ?? 25));
  params.set("offset", String(opts.offset ?? 0));
  return fetchJson<CaseListResponse>(`/api/cases?${params.toString()}`);
}

export function getCase(id: number): Promise<CaseDetail> {
  return fetchJson<CaseDetail>(`/api/cases/${id}`);
}
