"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

const OUTCOMES = ["SUCCESS", "FAILED", "STILL_PENDING", "NO_RESPONSE"] as const;

export function CaseControls({ caseId, status }: { caseId: number; status: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [outcome, setOutcome] = useState<(typeof OUTCOMES)[number]>("SUCCESS");

  const canRunAgent = status === "DETECTED" || status === "DIAGNOSING";
  const canExecute = status === "SAFETY_CHECK";
  const canOutcome = status === "WAITING_FOR_RESULT";
  const canVerify = status === "WAITING_FOR_RESULT";
  const terminal = ["RECOVERED", "NOT_RECOVERED", "STOPPED", "ESCALATED"].includes(status);

  async function call(path: string, body?: unknown) {
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const data = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      const text = data
        ? String(data["reason"] ?? data["decision"] ?? data["detail"] ?? `HTTP ${res.status}`)
        : `HTTP ${res.status}`;
      setMessage({ ok: res.ok, text });
      router.refresh();
    } catch (err) {
      setMessage({ ok: false, text: `request failed: ${String(err)}` });
    } finally {
      setBusy(false);
    }
  }

  if (terminal) {
    return (
      <p className="text-sm text-zinc-500">
        This case is terminal ({status}). The audit trail below is the complete record of what the agent did and why.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-zinc-500">
        Pipeline steps for this case — each step runs the real backend pipeline (agent → gate → executor → simulator →
        verification). Outcomes are simulated.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={!canRunAgent || busy}
          onClick={() => call(`/api/cases/${caseId}/agent/run`)}
          className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-40"
          title={canRunAgent ? "Diagnose, score, select an action and pass the safety gate" : "Agent runs on DETECTED or DIAGNOSING cases"}
        >
          Run agent
        </button>
        <button
          type="button"
          disabled={!canExecute || busy}
          onClick={() => call(`/api/cases/${caseId}/action/execute`)}
          className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-40"
          title={canExecute ? "Execute the gate-ALLOWed action (simulated payment/link + notification)" : "Execution requires a gate-ALLOWed case in SAFETY_CHECK"}
        >
          Execute action
        </button>
        <span className="flex items-center gap-1.5">
          <select
            value={outcome}
            disabled={!canOutcome || busy}
            onChange={(event) => setOutcome(event.target.value as (typeof OUTCOMES)[number])}
            className="rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm disabled:opacity-40"
            aria-label="Scripted outcome"
          >
            {OUTCOMES.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!canOutcome || busy}
            onClick={() => call(`/api/cases/${caseId}/outcome`, { outcome })}
            className="rounded-lg border border-amber-400 bg-amber-50 px-3 py-1.5 text-sm font-medium text-amber-800 hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-40"
            title={canOutcome ? "Inject the scripted outcome through the real event pipeline" : "Outcomes apply to WAITING_FOR_RESULT cases"}
          >
            Simulate outcome (simulated)
          </button>
        </span>
        <button
          type="button"
          disabled={!canVerify || busy}
          onClick={() => call(`/api/cases/${caseId}/verify`)}
          className="rounded-lg bg-zinc-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-900 disabled:cursor-not-allowed disabled:opacity-40"
          title={canVerify ? "Verify the outcome and attribute recovered revenue" : "Verification runs on WAITING_FOR_RESULT cases"}
        >
          Verify outcome
        </button>
      </div>
      {message ? (
        <p className={`text-xs ${message.ok ? "text-emerald-700" : "text-rose-700"}`}>
          {message.ok ? "✓" : "✕"} {message.text}
        </p>
      ) : null}
    </div>
  );
}
