"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Provider, ProviderKey } from "@/lib/api";

export default function ProvidersPage() {
  const [keys, setKeys] = useState<ProviderKey[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    try { setKeys(await api.providerKeys()); } catch (reason) {
      if (reason instanceof Error && reason.message.includes("Authentication required")) window.location.assign("/login");
      else setError(reason instanceof Error ? reason.message : "Could not load provider keys.");
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  async function addKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setError(""); setNotice(""); setBusy(true);
    try {
      await api.addProviderKey(String(data.get("provider")) as Provider, String(data.get("label")), String(data.get("api_key")));
      form.reset();
      setNotice("Provider key saved. The key itself cannot be viewed again.");
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not save provider key."); }
    finally { setBusy(false); }
  }

  async function testKey(key: ProviderKey) {
    setError(""); setNotice("");
    try {
      const result = await api.testProviderKey(key.id);
      setNotice(`${key.label}: API health check OK (${result.model}, ${result.tokens_in + result.tokens_out} tokens).`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Provider test failed."); }
  }

  async function removeKey(key: ProviderKey) {
    setError("");
    try { await api.removeProviderKey(key.id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not remove provider key."); }
  }

  return <main className="mx-auto min-h-screen max-w-3xl px-6 py-12">
    <header className="flex items-center justify-between"><a className="text-xl font-bold" href="/">Forge<span className="text-emerald-400">.</span></a><button className="text-sm text-slate-300" onClick={() => void api.logout().then(() => window.location.assign("/login"))}>Log out</button></header>
    <h1 className="mt-12 text-3xl font-semibold">Provider keys</h1>
    <p className="mt-2 text-slate-400">Connect your own model provider. Stored keys are write-only and never shown again.</p>
    <form onSubmit={addKey} className="mt-8 grid gap-4 rounded-xl border border-slate-800 bg-slate-900/60 p-6 sm:grid-cols-2">
      <label className="grid gap-2 text-sm">Provider<select name="provider" className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2"><option value="anthropic">Anthropic</option><option value="openai">OpenAI</option><option value="gemini">Google Gemini</option></select></label>
      <label className="grid gap-2 text-sm">Label<input name="label" required maxLength={100} placeholder="Personal account" className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2" /></label>
      <label className="grid gap-2 text-sm sm:col-span-2">API key<input name="api_key" type="password" autoComplete="new-password" required minLength={8} className="rounded-md border border-slate-700 bg-slate-950 px-3 py-2" /></label>
      <button disabled={busy} className="w-fit rounded-md bg-emerald-400 px-4 py-2 font-semibold text-slate-950 disabled:opacity-60">{busy ? "Saving…" : "Save provider key"}</button>
    </form>
    {error && <p role="alert" className="mt-4 text-sm text-red-300">{error}</p>}
    {notice && <p role="status" className="mt-4 text-sm text-emerald-300">{notice}</p>}
    <section aria-label="Saved provider keys" className="mt-10 grid gap-3">
      {keys.map((key) => <article key={key.id} className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-slate-800 p-4"><div><p className="font-medium">{key.label} <span className="text-slate-400">· {key.provider}</span></p><p className="text-sm text-slate-400">Key ending in {key.last4}</p></div><div className="flex gap-2"><button className="rounded border border-slate-700 px-3 py-1.5 text-sm" onClick={() => void testKey(key)}>Test</button><button className="rounded border border-red-900 px-3 py-1.5 text-sm text-red-300" onClick={() => void removeKey(key)}>Remove</button></div></article>)}
      {keys.length === 0 && <p className="text-sm text-slate-400">No provider keys saved yet.</p>}
    </section>
  </main>;
}
