import Link from "next/link";

export default function CaseNotFound() {
  return (
    <div className="rounded-xl border border-dashed border-zinc-300 bg-white p-8 text-center">
      <h1 className="text-lg font-semibold text-zinc-800">Case not found</h1>
      <p className="mt-1 text-sm text-zinc-500">This recovery case does not exist.</p>
      <Link href="/cases" className="mt-3 inline-block text-sm font-medium text-indigo-700 hover:underline">
        ← Back to all cases
      </Link>
    </div>
  );
}
