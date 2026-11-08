"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { api, type AutonomyLevel } from "@/lib/api";

const levels: { value: AutonomyLevel; title: string; description: string }[] = [
  { value: "step_by_step", title: "Step by step", description: "Review and approve each step before work continues." },
  { value: "milestones", title: "Milestones", description: "Let the agent work between milestones, with review at each checkpoint." },
  { value: "autopilot", title: "Autopilot", description: "Let the agent work independently within your budget and safety limits." },
];

function isAutonomyLevel(value: string): value is AutonomyLevel {
  return levels.some((item) => item.value === value);
}

export function validateRunForm(idea: string, level: string, budget: string): string | null {
  if (!idea.trim()) return "Enter an idea to continue.";
  if (!isAutonomyLevel(level)) return "Choose a valid autonomy level.";
  const amount = Number(budget);
  if (budget.trim() === "" || !Number.isFinite(amount) || amount < 0) {
    return "Enter a budget of zero or more.";
  }
  return null;
}

export default function NewRunPage() {
  const router = useRouter();
  const [idea, setIdea] = useState("");
  const [level, setLevel] = useState<AutonomyLevel>("milestones");
  const [budget, setBudget] = useState("25");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    const validationError = validateRunForm(idea, level, budget);
    if (validationError) {
      setError(validationError);
      return;
    }
    if (!isAutonomyLevel(level)) return;
    setSaving(true);
    try { const run = await api.createRun(idea.trim(), level, Number(budget)); router.push(`/runs/${run.id}`); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not create run."); }
    finally { setSaving(false); }
  }
  return <main className="mx-auto max-w-2xl space-y-8 p-8">
    <header><p className="text-sm text-slate-500">New project run</p><h1 className="text-3xl font-semibold">What should Forge build?</h1></header>
    <form onSubmit={submit} className="space-y-6">
      <label className="block space-y-2">Your idea<textarea required minLength={1} value={idea} onChange={(event) => setIdea(event.target.value)} className="min-h-32 w-full rounded border p-3" placeholder="Describe the app you want to build" /></label>
      <fieldset className="space-y-3"><legend className="font-medium">How much autonomy?</legend>{levels.map((item) => <label key={item.value} className="flex cursor-pointer gap-3 rounded border p-4"><input type="radio" name="autonomy" value={item.value} checked={level === item.value} onChange={() => setLevel(item.value)} /><span><strong className="block">{item.title}</strong><span className="text-sm text-slate-600">{item.description}</span></span></label>)}</fieldset>
      <label className="block space-y-2">Budget cap (USD)<input required type="number" min="0" step="0.01" value={budget} onChange={(event) => setBudget(event.target.value)} className="block rounded border p-2" /></label>
      {error && <p role="alert" className="text-red-700">{error}</p>}
      <button disabled={saving} className="rounded bg-slate-900 px-5 py-3 text-white">{saving ? "Creating…" : "Create run"}</button>
    </form>
  </main>;
}
