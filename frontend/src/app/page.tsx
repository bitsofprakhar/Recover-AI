import Link from "next/link";

import { ApiError, CaseListResponse, getCases, getMetrics, Metrics } from "@/lib/api";
import { formatDuration, formatInr, formatPercent, statusTone } from "@/lib/format";
import { BackendDown, EmptyState } from "@/components/StateViews";
import { CasesTable } from "@/components/CasesTable";
import { MetricCard } from "@/components/MetricCard";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  let metrics: Metrics;
  let recent: CaseListResponse;
  try {
    [metrics, recent] = await Promise.all([getMetrics(), getCases({ limit: 10, offset: 0 })]);
  } catch (err) {
    return <BackendDown error={err instanceof ApiError ? err.message : String(err)} />;
  }

  const totalCases = metrics.cases.total;
  const statusEntries = Object.entries(metrics.cases.by_status);
  const maxStatusCount = Math.max(1, ...statusEntries.map(([, count]) => count));
  const blockedByTool = Object.entries(metrics.invalid_or_blocked_actions.by_tool);

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-semibold tracking-tight">Revenue recovery overview</h1>
        <p className="mt-1 text-sm text-zinc-600">
          Detected → Diagnose → Score → Decide → Safety check → Act → Verify → Recover. All figures below are live,
          computed from stored case, payment and audit data.
        </p>
      </section>

      {totalCases === 0 ? (
        <EmptyState
          title="No recovery cases yet"
          hint="The pipeline creates a case when a failed payment with revenue at risk arrives — e.g. POST /api/events/synthetic with a failed payment, then run the agent from the case page."
        />
      ) : null}

      <section aria-label="Key metrics" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Revenue at risk (eligible)"
          value={formatInr(metrics.revenue_at_risk.eligible)}
          sub={`total ${formatInr(metrics.revenue_at_risk.total)} · escalated excluded ${formatInr(metrics.revenue_at_risk.escalated_excluded)}`}
          note={metrics.revenue_at_risk.note}
          tone="warning"
        />
        <MetricCard
          label="Recovered revenue"
          value={formatInr(metrics.recovered.revenue)}
          sub={`${metrics.recovered.cases} recovered ${metrics.recovered.cases === 1 ? "case" : "cases"}`}
          note="Verified successful amounts attributable to completed recovery cases (Phase 9 attribution)"
          tone="positive"
        />
        <MetricCard
          label="Recovery rate"
          value={formatPercent(metrics.recovery_rate)}
          sub="recovered revenue ÷ eligible revenue at risk"
          note="None when there is no eligible revenue at risk yet"
        />
        <MetricCard
          label="Active cases"
          value={metrics.cases.active}
          sub={`${totalCases} total`}
          note="Cases in a non-terminal state of the recovery state machine"
        />
      </section>

      <section aria-label="Operations metrics" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <MetricCard label="Recovery attempts" value={metrics.attempts.total} sub="act actions executed" />
        <MetricCard label="Successful recoveries" value={metrics.attempts.successful_recoveries} sub="RECOVERED cases" tone="positive" />
        <MetricCard
          label="Average recovery time"
          value={formatDuration(metrics.average_recovery_time?.seconds ?? null)}
          sub={
            metrics.average_recovery_time
              ? `mean over ${metrics.average_recovery_time.cases_counted} recovered ${metrics.average_recovery_time.cases_counted === 1 ? "case" : "cases"}`
              : "no completed recoveries yet"
          }
        />
        <MetricCard label="Escalation rate" value={formatPercent(metrics.escalation_rate)} sub="escalated ÷ total cases" tone="warning" />
        <MetricCard
          label="Invalid / blocked actions"
          value={metrics.invalid_or_blocked_actions.total}
          sub={blockedByTool.length > 0 ? blockedByTool.map(([tool, count]) => `${tool}: ${count}`).join(" · ") : "none blocked"}
          note="Gate decisions and tool calls the backend refused to execute"
          tone={metrics.invalid_or_blocked_actions.total > 0 ? "danger" : "default"}
        />
      </section>

      <section aria-label="Case status distribution" className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">Case status distribution</h2>
        {statusEntries.length === 0 ? (
          <p className="mt-2 text-sm text-zinc-500">No cases.</p>
        ) : (
          <ul className="mt-3 space-y-2">
            {statusEntries.map(([status, count]) => (
              <li key={status} className="flex items-center gap-3 text-sm">
                <span
                  className={`inline-flex w-44 justify-center rounded-full border px-2 py-0.5 text-xs font-medium ${statusTone(status)}`}
                >
                  {status}
                </span>
                <span className="w-8 text-right tabular-nums text-zinc-700">{count}</span>
                <span className="h-2 flex-1 overflow-hidden rounded-full bg-zinc-100">
                  <span
                    className={`block h-full rounded-full ${status === "RECOVERED" ? "bg-emerald-400" : "bg-zinc-400"}`}
                    style={{ width: `${Math.round((count / maxStatusCount) * 100)}%` }}
                  />
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Recent cases" className="rounded-xl border border-zinc-200 bg-white shadow-sm">
        <div className="flex items-center justify-between border-b border-zinc-100 px-5 py-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">Recent cases</h2>
          <Link href="/cases" className="text-sm font-medium text-indigo-700 hover:underline">
            View all cases →
          </Link>
        </div>
        <div className="p-3">
          {recent.items.length === 0 ? (
            <p className="px-2 py-6 text-sm text-zinc-500">No cases yet.</p>
          ) : (
            <CasesTable cases={recent.items} compact />
          )}
        </div>
      </section>
    </div>
  );
}
