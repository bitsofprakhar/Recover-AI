import { AgentActionRow, AuditLogRow } from "@/lib/api";
import { formatDateTimeUtc } from "@/lib/format";
import { SimulatedTag } from "@/components/SimulatedTag";

const AUDIT_LABELS: Record<string, string> = {
  "risk.case_created": "Recovery case created",
  "risk.case_escalated": "Case escalated (ambiguity)",
  "risk.case_duplicate": "Duplicate case suppressed",
  "risk.pending_verification": "Pending payment routed to verification",
  "risk.already_successful": "Already-successful payment ignored",
  "agent.diagnosis_started": "Agent started diagnosis",
  "agent.diagnosis_completed": "Diagnosis completed",
  "agent.scored": "Recovery score computed",
  "agent.action_selected": "Recovery action selected",
  "agent.case_stopped": "Case stopped (low score)",
  "agent.case_escalated": "Case escalated by agent",
  "agent.note": "Agent note",
  "gate.submitted": "Action submitted to safety gate",
  "gate.allowed": "Safety gate: ALLOW",
  "gate.blocked": "Safety gate: BLOCK",
  "gate.escalated": "Safety gate: ESCALATE",
  "gate.case_stopped": "Case stopped by gate",
  "action.executed": "Recovery action executed",
  "action.monitoring_started": "Monitoring started",
  "action.execution_failed": "Action execution failed",
  "outcome.success": "Outcome: success",
  "outcome.retry": "Outcome: failed — new attempt",
  "outcome.not_recovered": "Outcome: failed — attempts exhausted",
  "outcome.still_pending": "Outcome: still pending",
  "outcome.no_response": "Outcome: no response",
  "case.window_expired": "Case window expired",
  "verification.recovered": "Recovery verified & attributed",
  "verification.not_recovered": "Success not attributable — not recovered",
  "verification.escalated": "Verification escalated",
  "payment.created": "Payment created",
  "payment.status_changed": "Payment status changed",
  "order.status_changed": "Order status changed",
};

type Entry =
  | {
      kind: "audit";
      ts: string;
      eventType: string;
      from: string | null;
      to: string | null;
      payload: Record<string, unknown> | null;
    }
  | {
      kind: "action";
      ts: string;
      tool: string;
      allowed: boolean | null;
      input: Record<string, unknown> | null;
      output: Record<string, unknown> | null;
    };

function buildEntries(auditLogs: AuditLogRow[], agentActions: AgentActionRow[]): Entry[] {
  const entries: Entry[] = [
    ...auditLogs.map((row) => ({
      kind: "audit" as const,
      ts: row.created_at,
      eventType: row.event_type,
      from: row.from_status,
      to: row.to_status,
      payload: row.payload,
    })),
    ...agentActions.map((row) => ({
      kind: "action" as const,
      ts: row.created_at,
      tool: row.tool_name,
      allowed: row.allowed,
      input: row.input,
      output: row.output,
    })),
  ];
  return entries.sort((a, b) => (a.ts === b.ts ? 0 : a.ts < b.ts ? -1 : 1));
}

function pickString(obj: Record<string, unknown> | null | undefined, key: string): string | null {
  const value = obj?.[key];
  return typeof value === "string" && value ? value : null;
}

function pickNumber(obj: Record<string, unknown> | null | undefined, key: string): number | null {
  const value = obj?.[key];
  return typeof value === "number" ? value : null;
}

function isTerminal(toStatus: string | null): boolean {
  return toStatus === "RECOVERED" || toStatus === "NOT_RECOVERED" || toStatus === "STOPPED" || toStatus === "ESCALATED";
}

function markerTone(entry: Entry): string {
  if (entry.kind === "audit") {
    if (isTerminal(entry.to)) {
      if (entry.to === "RECOVERED") return "bg-emerald-500";
      if (entry.to === "NOT_RECOVERED") return "bg-rose-500";
      if (entry.to === "ESCALATED") return "bg-amber-500";
      return "bg-zinc-500";
    }
    return "bg-indigo-400";
  }
  if (entry.allowed === true) return "bg-emerald-500";
  if (entry.allowed === false) return "bg-rose-500";
  return "bg-zinc-400";
}

function auditDetail(entry: Extract<Entry, { kind: "audit" }>): { lines: string[]; simulated: boolean } {
  const lines: string[] = [];
  const payload = entry.payload ?? {};
  const reason = pickString(payload, "reason");
  if (reason) lines.push(`reason: ${reason}`);
  const detail = pickString(payload, "detail");
  if (detail) lines.push(`detail: ${detail}`);
  const note = pickString(payload, "note");
  if (note) lines.push(`note: ${note}`);
  const action = pickString(payload, "action");
  if (action) lines.push(`action: ${action}`);
  const alternative = pickString(payload, "alternative");
  if (alternative) lines.push(`safe alternative: ${alternative}`);
  const score = pickNumber(payload, "score");
  const band = pickString(payload, "band");
  if (score !== null || band) lines.push(`score: ${score ?? "—"}${band ? ` · ${band} band` : ""}`);
  const selected = pickString(payload, "selected_action");
  if (selected) lines.push(`selected action: ${selected}`);
  const payment = pickString(payload, "payment_id");
  if (payment) lines.push(`payment: ${payment}`);
  const amount = pickString(payload, "amount");
  if (amount) lines.push(`amount: ${amount}`);
  const attempts = pickNumber(payload, "attempt_count");
  if (attempts !== null) lines.push(`attempt: ${attempts}`);
  return { lines, simulated: payload["simulated"] === true };
}

const ACTION_SUMMARY_KEYS = [
  "decision",
  "reason",
  "result",
  "status",
  "error",
  "detail",
  "payment_id",
  "recovery_payment_id",
  "recovery_link_id",
  "channel",
  "recipient_masked",
  "score",
  "band",
  "executed",
] as const;

function actionDetail(entry: Extract<Entry, { kind: "action" }>): { lines: string[]; simulated: boolean } {
  const output = entry.output ?? {};
  const lines: string[] = [];
  for (const key of ACTION_SUMMARY_KEYS) {
    const value = output[key];
    if (value === undefined || value === null || value === "") continue;
    if (key === "executed") {
      if (value === false && (output["reason"] || output["error"])) continue; // already covered by reason/error
      lines.push(`executed: ${String(value)}`);
      continue;
    }
    lines.push(`${key.replace(/_/g, " ")}: ${String(value)}`);
    if (lines.length >= 4 && key !== "reason") break;
  }
  return { lines, simulated: output["simulated"] === true };
}

function AuditChips({ entry }: { entry: Extract<Entry, { kind: "audit" }> }) {
  if (!entry.from && !entry.to) return null;
  return (
    <span className="inline-flex items-center gap-1 text-xs text-zinc-500">
      {entry.from ? <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[10px]">{entry.from}</span> : null}
      {entry.from ? <span aria-hidden>→</span> : null}
      {entry.to ? (
        <span
          className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
            isTerminal(entry.to) && entry.to !== "STOPPED" && entry.to !== "ESCALATED"
              ? "bg-zinc-800 text-white"
              : "bg-zinc-100"
          }`}
        >
          {entry.to}
        </span>
      ) : null}
    </span>
  );
}

function AllowedChip({ allowed }: { allowed: boolean | null }) {
  if (allowed === true) {
    return (
      <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-700">
        allowed
      </span>
    );
  }
  if (allowed === false) {
    return (
      <span className="rounded-full border border-rose-200 bg-rose-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-rose-700">
        blocked
      </span>
    );
  }
  return (
    <span className="rounded-full border border-zinc-200 bg-zinc-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
      read
    </span>
  );
}

function TimelineRow({ entry }: { entry: Entry }) {
  const detail = entry.kind === "audit" ? auditDetail(entry) : actionDetail(entry);
  const title =
    entry.kind === "audit" ? (AUDIT_LABELS[entry.eventType] ?? entry.eventType) : `tool call: ${entry.tool}`;
  const raw = entry.kind === "audit" ? entry.payload : entry.output;
  const rawLabel = entry.kind === "audit" ? "audit payload" : "tool output";
  return (
    <li className="relative pl-8 pb-6 last:pb-0">
      <span className={`absolute left-1.5 top-1.5 h-2.5 w-2.5 rounded-full ring-4 ring-white ${markerTone(entry)}`} />
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-zinc-900">{title}</span>
        {entry.kind === "action" ? <AllowedChip allowed={entry.allowed} /> : null}
        {detail.simulated ? <SimulatedTag /> : null}
        <span className="ml-auto text-xs tabular-nums text-zinc-400">{formatDateTimeUtc(entry.ts)}</span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        {entry.kind === "audit" ? <AuditChips entry={entry} /> : null}
      </div>
      {detail.lines.length > 0 ? (
        <ul className="mt-1.5 space-y-0.5">
          {detail.lines.map((line) => (
            <li key={line} className="text-xs text-zinc-600">
              {line}
            </li>
          ))}
        </ul>
      ) : null}
      {raw && Object.keys(raw).length > 0 ? (
        <details className="mt-1.5">
          <summary className="cursor-pointer select-none text-xs text-zinc-400 hover:text-zinc-600">
            {rawLabel}
          </summary>
          <pre className="mt-1 max-w-2xl overflow-x-auto rounded-lg bg-zinc-50 p-2 text-[11px] leading-relaxed text-zinc-600">
            {JSON.stringify(raw, null, 2)}
          </pre>
        </details>
      ) : null}
    </li>
  );
}

export function Timeline({ auditLogs, agentActions }: { auditLogs: AuditLogRow[]; agentActions: AgentActionRow[] }) {
  const entries = buildEntries(auditLogs, agentActions);
  if (entries.length === 0) {
    return <p className="text-sm text-zinc-500">No timeline entries yet.</p>;
  }
  return (
    <ol className="relative border-l border-zinc-200 pl-0">
      {entries.map((entry, index) => (
        <TimelineRow key={`${entry.kind}-${entry.ts}-${index}`} entry={entry} />
      ))}
    </ol>
  );
}
