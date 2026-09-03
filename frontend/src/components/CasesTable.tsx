import Link from "next/link";

import { CaseSummary } from "@/lib/api";
import { formatInr, timeRemaining } from "@/lib/format";
import { ScoreBadge } from "@/components/ScoreBadge";
import { StatusBadge } from "@/components/StatusBadge";

function DeadlineCell({ expiry, status }: { expiry: string; status: string }) {
  const deadline = timeRemaining(expiry);
  const terminal = ["RECOVERED", "NOT_RECOVERED", "STOPPED", "ESCALATED"].includes(status);
  if (terminal) {
    return <span className="text-zinc-400">closed</span>;
  }
  return (
    <span className={deadline.expired ? "text-rose-600 font-medium" : "text-zinc-600 tabular-nums"}>
      {deadline.text}
    </span>
  );
}

export function CasesTable({ cases, compact = false }: { cases: CaseSummary[]; compact?: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="border-b border-zinc-200 text-left text-xs uppercase tracking-wide text-zinc-500">
            <th className="px-3 py-2">Case</th>
            <th className="px-3 py-2">Status</th>
            <th className="px-3 py-2 text-right">Amount at risk</th>
            <th className="px-3 py-2">Method</th>
            {compact ? null : <th className="px-3 py-2">Failure reason</th>}
            <th className="px-3 py-2">Score</th>
            {compact ? null : <th className="px-3 py-2 text-center">Attempts</th>}
            <th className="px-3 py-2">Deadline</th>
            <th className="px-3 py-2 text-right">Recovered</th>
          </tr>
        </thead>
        <tbody>
          {cases.map((item) => (
            <tr key={item.id} className="border-b border-zinc-100 hover:bg-zinc-50">
              <td className="px-3 py-2 font-medium">
                <Link href={`/cases/${item.id}`} className="text-indigo-700 hover:underline">
                  #{item.id}
                </Link>
              </td>
              <td className="px-3 py-2">
                <StatusBadge status={item.status} />
              </td>
              <td className="px-3 py-2 text-right tabular-nums">{formatInr(item.revenue_at_risk)}</td>
              <td className="px-3 py-2">{item.payment?.method ?? "—"}</td>
              {compact ? null : (
                <td className="px-3 py-2 text-zinc-600">{item.payment?.failure_reason ?? "—"}</td>
              )}
              <td className="px-3 py-2">
                <ScoreBadge score={item.score} />
              </td>
              {compact ? null : (
                <td className="px-3 py-2 text-center tabular-nums">{item.attempt_count}</td>
              )}
              <td className="px-3 py-2">
                <DeadlineCell expiry={item.expiry} status={item.status} />
              </td>
              <td className="px-3 py-2 text-right tabular-nums">
                {item.recovered_amount ? (
                  <span className="text-emerald-700 font-medium">{formatInr(item.recovered_amount)}</span>
                ) : (
                  <span className="text-zinc-400">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
