export function SimulatedTag({ label = "simulated" }: { label?: string }) {
  return (
    <span
      className="inline-flex items-center rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700"
      title="Simulated: no real payment operation or delivery occurred — this is the demo/test outcome injector"
    >
      {label}
    </span>
  );
}
