export function BackendDown({ error }: { error: string }) {
  return (
    <div className="rounded-xl border border-rose-200 bg-rose-50 p-6">
      <h2 className="text-base font-semibold text-rose-800">Backend unreachable</h2>
      <p className="mt-1 text-sm text-rose-700">
        The dashboard renders live data from the FastAPI backend; no data is cached or faked.
      </p>
      <p className="mt-2 text-sm text-rose-700">
        Start it from <code className="rounded bg-white/70 px-1 py-0.5 font-mono text-xs">backend/</code>:
        <code className="ml-2 rounded bg-white/70 px-1 py-0.5 font-mono text-xs">
          .venv\Scripts\python -m uvicorn main:app
        </code>
      </p>
      <p className="mt-2 text-xs text-rose-600">{error}</p>
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="rounded-xl border border-dashed border-zinc-300 bg-white p-8 text-center">
      <p className="text-sm font-medium text-zinc-700">{title}</p>
      <p className="mt-1 text-xs text-zinc-500">{hint}</p>
    </div>
  );
}
