import { scoreBand } from "@/lib/format";

const BAND_TONE: Record<string, string> = {
  HIGH: "bg-emerald-100 text-emerald-800 border-emerald-200",
  MEDIUM: "bg-amber-100 text-amber-800 border-amber-200",
  LOW: "bg-rose-100 text-rose-800 border-rose-200",
};

export function ScoreBadge({ score }: { score: number | null }) {
  const band = scoreBand(score);
  if (score === null || score === undefined || band === null) {
    return <span className="text-zinc-400">—</span>;
  }
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium whitespace-nowrap ${BAND_TONE[band]}`}
      title={`score ${score} → ${band} band (HIGH ≥ 80, MEDIUM ≥ 35, LOW < 35)`}
    >
      {score} · {band}
    </span>
  );
}
