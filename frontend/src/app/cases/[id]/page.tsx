import Link from "next/link";
import { notFound } from "next/navigation";

import { ApiError, CaseDetail, getCase } from "@/lib/api";
import { formatDateTimeUtc, formatInr, timeRemaining } from "@/lib/format";
import { BackendDown } from "@/components/StateViews";
import { CaseControls } from "@/components/CaseControls";
import { ScoreBadge } from "@/components/ScoreBadge";
import { SimulatedTag } from "@/components/SimulatedTag";
import { StatusBadge } from "@/components/StatusBadge";
import { Timeline } from "@/components/Timeline";

export const dynamic = "force-dynamic";

function StatTile({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm">
      <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">{label}</div>
      <div className="mt-1.5 text-xl font-semibold tabular-nums text-zinc-900">{value}</div>
      {sub ? <div className="mt-1 text-xs text-zinc-500">{sub}</div> : null}
    </div>
  );
}

function ContextCard({ title, rows }: { title: string; rows: [string, React.ReactNode][] }) {
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm">
      <h2 className="text-sm font-semibold text-zinc-700">{title}</h2>
      <dl className="mt-2 space-y-1.5 text-sm">
        {rows.map(([term, value]) => (
          <div key={term} className="flex justify-between gap-4">
            <dt className="text-zinc-500">{term}</dt>
            <dd className="text-right font-medium text-zinc-800">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export default async function CaseDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const numericId = Number(id);
  if (!Number.isInteger(numericId) || numericId <= 0) {
    notFound();
  }

  let detail: CaseDetail;
  try {
    detail = await getCase(numericId);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    return <BackendDown error={err instanceof ApiError ? err.message : String(err)} />;
  }

  const deadline = timeRemaining(detail.expiry);
  const terminal = ["RECOVERED", "NOT_RECOVERED", "STOPPED", "ESCALATED"].includes(detail.status);

  return (
    <div className="space-y-6">
      <nav className="text-sm text-zinc-500">
        <Link href="/cases" className="hover:text-zinc-900">
          ← All cases
        </Link>
      </nav>

      <section className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Case #{detail.id}</h1>
        <StatusBadge status={detail.status} />
        {detail.selected_action ? (
          <span className="rounded-full border border-zinc-200 bg-white px-2.5 py-0.5 text-xs font-medium text-zinc-600">
            action: {detail.selected_action}
          </span>
        ) : null}
        <span className="ml-auto text-xs text-zinc-500">created {formatDateTimeUtc(detail.created_at)}</span>
      </section>

      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <StatTile label="Revenue at risk" value={formatInr(detail.revenue_at_risk)} sub="authoritative order amount" />
        <StatTile
          label="Recovered"
          value={detail.recovered_amount ? (
            <span className="text-emerald-700">{formatInr(detail.recovered_amount)}</span>
          ) : (
            <span className="text-zinc-400">—</span>
          )}
          sub={detail.recovered_payment_id ? `via ${detail.recovered_payment_id}` : "not credited yet"}
        />
        <StatTile label="Score" value={<ScoreBadge score={detail.score} />} sub="deterministic 0–100 likelihood" />
        <StatTile label="Attempts" value={detail.attempt_count} sub="act actions executed (max 2)" />
        <StatTile
          label="Deadline (case window)"
          value={
            terminal ? (
              <span className="text-zinc-400">closed</span>
            ) : deadline.expired ? (
              <span className="text-rose-600">expired</span>
            ) : (
              <span className="text-sky-700">{deadline.text}</span>
            )
          }
          sub={`expires ${formatDateTimeUtc(detail.expiry)}`}
        />
      </section>

      {detail.status === "RECOVERED" ? (
        <section className="rounded-xl border border-emerald-200 bg-emerald-50 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-emerald-800">Recovery verified &amp; attributed</h2>
            <SimulatedTag />
          </div>
          <dl className="mt-2 grid gap-x-8 gap-y-1.5 text-sm sm:grid-cols-2">
            <div className="flex justify-between gap-4">
              <dt className="text-emerald-700">recovered payment</dt>
              <dd className="font-mono text-xs text-emerald-900">{detail.recovered_payment_id}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-emerald-700">amount</dt>
              <dd className="font-medium text-emerald-900">{formatInr(detail.recovered_amount)}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-emerald-700">captured at</dt>
              <dd className="text-emerald-900">{formatDateTimeUtc(detail.recovered_at)}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-emerald-700">credited</dt>
              <dd className="text-emerald-900">only after Phase 9 verification — never on capture alone</dd>
            </div>
          </dl>
          <p className="mt-2 text-xs text-emerald-700">
            Full attribution evidence (checks, event source, timing) is recorded in the{" "}
            <code className="rounded bg-white/60 px-1 font-mono">verification.recovered</code> entry of the timeline
            below.
          </p>
        </section>
      ) : null}

      <section className="grid gap-4 lg:grid-cols-3">
        <ContextCard
          title="Payment (source)"
          rows={[
            ["payment id", <span key="p" className="font-mono text-xs">{detail.payment?.payment_id ?? "—"}</span>],
            ["status", detail.payment?.status ?? "—"],
            ["amount", formatInr(detail.payment?.amount)],
            ["method", detail.payment?.method ?? "—"],
            ["failure reason", detail.payment?.failure_reason ?? "—"],
          ]}
        />
        <ContextCard
          title="Order"
          rows={[
            ["order id", <span key="o" className="font-mono text-xs">{detail.order?.order_id ?? "—"}</span>],
            ["status", detail.order?.status ?? "—"],
            ["amount", formatInr(detail.order?.amount)],
            ["currency", detail.order?.currency ?? "—"],
          ]}
        />
        <ContextCard
          title="Customer"
          rows={[
            ["customer id", <span key="c" className="font-mono text-xs">{detail.customer?.customer_id ?? "—"}</span>],
            ["name", detail.customer?.name ?? "—"],
            ["email", detail.customer?.email ?? "—"],
            ["phone", detail.customer?.phone ?? "—"],
          ]}
        />
      </section>

      <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">Pipeline controls</h2>
        <div className="mt-3">
          <CaseControls caseId={detail.id} status={detail.status} />
        </div>
      </section>

      <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">AI timeline &amp; audit trail</h2>
          <p className="text-xs text-zinc-500">
            detected → diagnosed → scored → action selected → safety result → executed → verified
          </p>
        </div>
        <div className="mt-4">
          <Timeline auditLogs={detail.audit_logs} agentActions={detail.agent_actions} />
        </div>
      </section>
    </div>
  );
}
