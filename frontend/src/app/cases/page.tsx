import Link from "next/link";

import { ApiError, CASE_STATUSES, CaseListResponse, getCases } from "@/lib/api";
import { statusTone } from "@/lib/format";
import { BackendDown, EmptyState } from "@/components/StateViews";
import { CasesTable } from "@/components/CasesTable";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 25;

export default async function CasesPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string; page?: string }>;
}) {
  const params = await searchParams;
  const status =
    params.status && CASE_STATUSES.includes(params.status as (typeof CASE_STATUSES)[number]) ? params.status : undefined;
  const requestedPage = Math.max(1, Number(params.page ?? 1) || 1);

  let data: CaseListResponse;
  try {
    data = await getCases({ status, limit: PAGE_SIZE, offset: (requestedPage - 1) * PAGE_SIZE });
  } catch (err) {
    return <BackendDown error={err instanceof ApiError ? err.message : String(err)} />;
  }

  const page = data.total === 0 ? 1 : Math.min(requestedPage, Math.max(1, Math.ceil(data.total / PAGE_SIZE)));
  const hasPrev = page > 1;
  const hasNext = page * PAGE_SIZE < data.total;
  const baseQuery = status ? { status } : {};

  return (
    <div className="space-y-6">
      <section className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Recovery cases</h1>
          <p className="mt-1 text-sm text-zinc-600">
            {data.total} {data.total === 1 ? "case" : "cases"}
            {status ? ` with status ${status}` : ""} · amount at risk, payment method, failure reason, score, current
            status and the 24-hour deadline
          </p>
        </div>
      </section>

      <nav aria-label="Filter by status" className="flex flex-wrap gap-2">
        <Link
          href="/cases"
          className={`rounded-full border px-3 py-1 text-xs font-medium ${status ? "border-zinc-200 bg-white text-zinc-600 hover:bg-zinc-50" : "border-zinc-400 bg-zinc-800 text-white"}`}
        >
          All
        </Link>
        {CASE_STATUSES.map((item) => (
          <Link
            key={item}
            href={`/cases?status=${item}`}
            className={`rounded-full border px-3 py-1 text-xs font-medium ${status === item ? "border-zinc-400 bg-zinc-800 text-white" : statusTone(item) + " hover:brightness-95"}`}
          >
            {item}
          </Link>
        ))}
      </nav>

      {data.items.length === 0 ? (
        <EmptyState
          title={status ? `No cases with status ${status}` : "No recovery cases yet"}
          hint="Cases appear here when a failed payment with revenue at risk arrives through the event pipeline."
        />
      ) : (
        <section className="rounded-xl border border-zinc-200 bg-white p-3 shadow-sm">
          <CasesTable cases={data.items} />
        </section>
      )}

      <nav aria-label="Pagination" className="flex items-center justify-between text-sm">
        <span className="text-zinc-500 tabular-nums">
          page {page} of {Math.max(1, Math.ceil(data.total / PAGE_SIZE))}
        </span>
        <span className="flex gap-2">
          {hasPrev ? (
            <Link
              href={{ pathname: "/cases", query: { ...baseQuery, page: page - 1 } }}
              className="rounded-lg border border-zinc-300 bg-white px-3 py-1.5 font-medium hover:bg-zinc-50"
            >
              ← Previous
            </Link>
          ) : null}
          {hasNext ? (
            <Link
              href={{ pathname: "/cases", query: { ...baseQuery, page: page + 1 } }}
              className="rounded-lg border border-zinc-300 bg-white px-3 py-1.5 font-medium hover:bg-zinc-50"
            >
              Next →
            </Link>
          ) : null}
        </span>
      </nav>
    </div>
  );
}
