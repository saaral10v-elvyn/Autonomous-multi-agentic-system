"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { api, type Plan, type Run, type RunEvent, type RunQuestion } from "@/lib/api";
import { useRunStream } from "@/hooks/use-run-stream";

function isQuestion(value: unknown): value is RunQuestion {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return typeof item.id === "string" && typeof item.prompt === "string" &&
    (item.type === "single_choice" || item.type === "multi_choice" || item.type === "free_text") &&
    Array.isArray(item.options) && typeof item.allow_decide_for_me === "boolean";
}

function isPlan(value: unknown): value is Plan {
  if (typeof value !== "object" || value === null) return false;
  const plan = value as Record<string, unknown>;
  return typeof plan.product_brief === "string" && Array.isArray(plan.tech_stack) &&
    Array.isArray(plan.milestones) && Array.isArray(plan.tasks);
}

function eventPlan(events: RunEvent[]): Plan | null {
  for (const event of [...events].reverse()) {
    if (event.type !== "plan.created" && event.type !== "plan.updated") continue;
    if (isPlan(event.payload.plan)) return event.payload.plan;
  }
  return null;
}

export default function RunPage() {
  const params = useParams<{ id: string }>();
  const runId = params.id;
  const { events, connected } = useRunStream(runId);
  const [run, setRun] = useState<Run | null>(null);
  const [answers, setAnswers] = useState<Record<string, unknown>>({});
  const [planDraft, setPlanDraft] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void api.run(runId).then(setRun).catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "Could not load run."),
    );
  }, [runId]);

  const state = [...events].reverse().find((event) => event.type === "run.state_changed")?.payload.status;
  const status = typeof state === "string" ? state : run?.status ?? "Loading";
  const questionEvent = [...events].reverse().find((event) => event.type === "question.asked");
  const rawQuestions = questionEvent?.payload.questions;
  const questions = Array.isArray(rawQuestions) ? rawQuestions.filter(isQuestion) : [];
  const plan = useMemo(() => eventPlan(events), [events]);

  useEffect(() => {
    if (plan) setPlanDraft(JSON.stringify(plan, null, 2));
  }, [plan]);

  async function submitAnswers(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      setRun(await api.submitRunAnswers(runId, answers));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not submit answers.");
    } finally {
      setSaving(false);
    }
  }

  async function savePlan() {
    setSaving(true);
    setError("");
    try {
      let parsed: unknown;
      try {
        parsed = JSON.parse(planDraft);
      } catch {
        throw new Error("Plan must be valid JSON.");
      }
      if (!isPlan(parsed)) throw new Error("Plan does not match the required structure.");
      await api.updateRunPlan(runId, parsed);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save plan.");
    } finally {
      setSaving(false);
    }
  }

  async function approvePlan() {
    setSaving(true);
    setError("");
    try {
      setRun(await api.approveRunPlan(runId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not approve plan.");
    } finally {
      setSaving(false);
    }
  }

  return <main className="mx-auto max-w-4xl space-y-6 p-8">
    <Link href="/runs" className="text-sm underline">← All runs</Link>
    {error && <p role="alert" className="rounded border border-red-300 p-3 text-red-700">{error}</p>}
    {run ? <>
      <header className="space-y-2">
        <p className="text-sm text-slate-500">{connected ? "Live" : "Reconnecting"} · <span aria-label="Run state">{status}</span></p>
        <h1 className="text-3xl font-semibold">{run.idea}</h1>
        <p>Budget cap: ${run.budget_usd}</p>
      </header>
      {status === "AWAITING_ANSWERS" && questions.length > 0 && <section className="space-y-4 rounded-xl border p-5">
        <h2 className="text-xl font-medium">A few questions</h2>
        <form className="space-y-5" onSubmit={(event) => void submitAnswers(event)}>
          {questions.map((question) => <fieldset key={question.id} className="space-y-2">
            <legend className="font-medium">{question.prompt}</legend>
            {question.type === "free_text" ? <textarea
              aria-label={question.prompt}
              className="min-h-24 w-full rounded border p-3 text-slate-900"
              value={typeof answers[question.id] === "string" && answers[question.id] !== "decide_for_me" ? String(answers[question.id]) : ""}
              onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: event.target.value }))}
            /> : question.type === "single_choice" ? <select
              aria-label={question.prompt}
              className="rounded border p-2 text-slate-900"
              value={typeof answers[question.id] === "string" ? String(answers[question.id]) : ""}
              onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: event.target.value }))}
            ><option value="">Choose one</option>{question.options.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}</select> : <div className="space-y-1">
              {question.options.map((option) => <label key={option.id} className="flex gap-2">
                <input type="checkbox" checked={Array.isArray(answers[question.id]) && (answers[question.id] as string[]).includes(option.id)} onChange={(event) => {
                  const selected = Array.isArray(answers[question.id]) ? answers[question.id] as string[] : [];
                  setAnswers((current) => ({ ...current, [question.id]: event.target.checked ? [...selected, option.id] : selected.filter((item) => item !== option.id) }));
                }} />{option.label}
              </label>)}
            </div>}
            {question.allow_decide_for_me && <button type="button" className="text-sm underline" onClick={() => setAnswers((current) => ({ ...current, [question.id]: "decide_for_me" }))}>Decide for me</button>}
          </fieldset>)}
          <button disabled={saving} className="rounded bg-slate-900 px-4 py-2 text-white">{saving ? "Submitting…" : "Submit answers"}</button>
        </form>
      </section>}
      {plan && <section className="space-y-4 rounded-xl border p-5">
        <h2 className="text-xl font-medium">Project plan</h2>
        <p>{plan.product_brief}</p>
        <div><h3 className="font-medium">Tech stack</h3><p>{plan.tech_stack.join(", ")}</p></div>
        <div><h3 className="font-medium">Milestones</h3><ul className="list-inside list-disc">{plan.milestones.map((milestone) => <li key={milestone.id}>{milestone.title}: {milestone.description}</li>)}</ul></div>
        {status === "AWAITING_PLAN_APPROVAL" && <div className="space-y-3">
          <label className="block font-medium" htmlFor="plan-editor">Edit plan JSON</label>
          <textarea id="plan-editor" className="min-h-72 w-full rounded border p-3 font-mono text-sm text-slate-900" value={planDraft} onChange={(event) => setPlanDraft(event.target.value)} />
          <div className="flex gap-3"><button type="button" disabled={saving} onClick={() => void savePlan()} className="rounded border px-4 py-2">Save edits</button><button type="button" disabled={saving} onClick={() => void approvePlan()} className="rounded bg-slate-900 px-4 py-2 text-white">Approve plan</button></div>
        </div>}
      </section>}
      <section className="space-y-3">
        <h2 className="text-xl font-medium">Run activity</h2>
        {events.length === 0 ? <p className="text-slate-600">Waiting for events…</p> : events.map((event) => <EventCard key={event.seq} event={event} />)}
      </section>
    </> : !error && <p>Loading run…</p>}
  </main>;
}

function EventCard({ event }: { event: RunEvent }) {
  return <article className="rounded border p-4"><p className="text-sm text-slate-500">#{event.seq} · {event.type}</p><pre className="mt-2 whitespace-pre-wrap text-sm">{JSON.stringify(event.payload, null, 2)}</pre></article>;
}
