/** Pure display helpers for the RecoverAI dashboard (unit-tested in format.test.ts). */

export function formatInr(amount: string | number | null | undefined): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const n = typeof amount === "string" ? Number(amount) : amount;
  if (!Number.isFinite(n)) return "—";
  return "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function formatPercent(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

export interface Deadline {
  text: string;
  expired: boolean;
}

export function timeRemaining(expiryIso: string | null | undefined, now: Date = new Date()): Deadline {
  if (!expiryIso) return { text: "—", expired: false };
  const expiry = new Date(expiryIso);
  if (Number.isNaN(expiry.getTime())) return { text: "—", expired: false };
  const ms = expiry.getTime() - now.getTime();
  if (ms <= 0) return { text: "expired", expired: true };
  const totalMinutes = Math.floor(ms / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours > 0) return { text: `${hours}h ${minutes}m left`, expired: false };
  return { text: `${minutes}m left`, expired: false };
}

export function formatDateTimeUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
}

export type ScoreBand = "HIGH" | "MEDIUM" | "LOW";

export function scoreBand(score: number | null | undefined): ScoreBand | null {
  if (score === null || score === undefined) return null;
  if (score >= 80) return "HIGH";
  if (score >= 35) return "MEDIUM";
  return "LOW";
}

export function statusTone(status: string): string {
  switch (status) {
    case "RECOVERED":
      return "bg-emerald-100 text-emerald-800 border-emerald-200";
    case "NOT_RECOVERED":
      return "bg-rose-100 text-rose-800 border-rose-200";
    case "ESCALATED":
      return "bg-amber-100 text-amber-800 border-amber-200";
    case "STOPPED":
      return "bg-zinc-200 text-zinc-700 border-zinc-300";
    case "WAITING_FOR_RESULT":
      return "bg-sky-100 text-sky-800 border-sky-200";
    case "ACTION_EXECUTED":
    case "SAFETY_CHECK":
      return "bg-indigo-100 text-indigo-800 border-indigo-200";
    case "ACTION_SELECTED":
    case "SCORED":
    case "DIAGNOSING":
      return "bg-violet-100 text-violet-800 border-violet-200";
    case "DETECTED":
      return "bg-orange-100 text-orange-800 border-orange-200";
    default:
      return "bg-zinc-100 text-zinc-700 border-zinc-200";
  }
}
