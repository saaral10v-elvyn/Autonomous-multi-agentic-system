"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type Run } from "@/lib/api";

export default function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    void api.runs().then(setRuns).catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "Could not load runs."),
    );
  }, []);
  return <main className="mx-auto max-w-3xl space-y-6 p-8">
    <div className="flex items-center justify-between"><h1 className="text-3xl font-semibold">Your runs</h1><Link href="/new" className="rounded bg-slate-900 px-4 py-2 text-white">New run</Link></div>
    {error && <p role="alert">{error}</p>}
    {runs.length === 0 && !error ? <p className="text-slate-600">No runs yet.</p> : <ul className="space-y-3">{runs.map((run) => <li key={run.id} className="rounded border p-4"><Link className="font-medium underline" href={`/runs/${run.id}`}>{run.idea}</Link><p className="text-sm text-slate-600">{run.status} · {run.autonomy_level} · ${run.budget_usd}</p></li>)}</ul>}
  </main>;
}
