import { ReactNode } from "react";

export function MetricCard({
  label,
  value,
  sub,
  note,
  tone,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  note?: string;
  tone?: "default" | "positive" | "warning" | "danger";
}) {
  const valueTone =
    tone === "positive"
      ? "text-emerald-700"
      : tone === "warning"
        ? "text-amber-700"
        : tone === "danger"
          ? "text-rose-700"
          : "text-zinc-900";
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm" title={note}>
      <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`mt-2 text-3xl font-semibold tabular-nums ${valueTone}`}>{value}</div>
      {sub ? <div className="mt-1 text-xs text-zinc-500">{sub}</div> : null}
    </div>
  );
}
