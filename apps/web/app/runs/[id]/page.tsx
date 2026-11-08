"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Run } from "@/lib/api";
import { useRunStream } from "@/hooks/use-run-stream";

export default function RunPage() {
  const params = useParams<{ id: string }>();
  const runId = params.id;
  const { events, connected } = useRunStream(runId);
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    void api.run(runId).then(setRun).catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "Could not load run."),
    );
  }, [runId]);
  return <main className="mx-auto max-w-3xl space-y-6 p-8">
    <Link href="/runs" className="text-sm underline">← All runs</Link>
    {error && <p role="alert">{error}</p>}
    {run ? <><header><p className="text-sm text-slate-500">{connected ? "Live" : "Reconnecting"} · {run.status}</p><h1 className="text-3xl font-semibold">{run.idea}</h1><p>Budget cap: ${run.budget_usd}</p></header><section className="space-y-3"><h2 className="text-xl font-medium">Run activity</h2>{events.length === 0 ? <p className="text-slate-600">Waiting for events…</p> : events.map((event) => <article key={event.seq} className="rounded border p-4"><p className="text-sm text-slate-500">#{event.seq} · {event.type}</p><pre className="mt-2 whitespace-pre-wrap text-sm">{JSON.stringify(event.payload, null, 2)}</pre></article>)}</section></> : !error && <p>Loading run…</p>}
  </main>;
}
