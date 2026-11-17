"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, type Integration, type IntegrationKind } from "@/lib/api";

export default function IntegrationsPage() {
  const [items, setItems] = useState<Integration[]>([]);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    try { setItems(await api.integrations()); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load integrations."); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setError(""); setNotice(""); setBusy(true);
    try {
      await api.connectIntegration(String(data.get("kind")) as IntegrationKind, String(data.get("label")), String(data.get("token")));
      form.reset(); setNotice("Integration connected. The token is encrypted and cannot be viewed again."); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not connect integration."); }
    finally { setBusy(false); }
  }
  async function remove(item: Integration) {
    try { await api.removeIntegration(item.id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not remove integration."); }
  }
  return <main className="mx-auto min-h-screen max-w-3xl px-6 py-12">
    <Link className="text-xl font-bold" href="/">Forge.</Link>
    <h1 className="mt-12 text-3xl font-semibold">Integrations</h1>
    <p className="mt-2 text-slate-400">Connect GitHub and Vercel tokens. Tokens are write-only and decrypted only by their service adapters.</p>
    <form onSubmit={connect} className="mt-8 grid gap-4 rounded-xl border p-6 sm:grid-cols-2">
      <label className="grid gap-2">Service<select name="kind"><option value="github">GitHub</option><option value="vercel">Vercel</option></select></label>
      <label className="grid gap-2">Account label<input name="label" required maxLength={100} /></label>
      <label className="grid gap-2 sm:col-span-2">Access token<input name="token" type="password" autoComplete="new-password" required minLength={8} /></label>
      <button disabled={busy} className="w-fit rounded bg-emerald-400 px-4 py-2 font-semibold text-slate-950">{busy ? "Connecting…" : "Connect"}</button>
    </form>
    {error && <p role="alert" className="mt-4 text-red-300">{error}</p>}
    {notice && <p role="status" className="mt-4 text-emerald-300">{notice}</p>}
    <section aria-label="Connected integrations" className="mt-8 grid gap-3">
      {items.map((item) => <article key={item.id} className="flex justify-between rounded border p-4">
        <p>{item.account_label} · {item.kind} · token ending {item.last4}</p>
        <button onClick={() => void remove(item)}>Remove</button>
      </article>)}
      {items.length === 0 && <p>No integrations connected yet.</p>}
    </section>
  </main>;
}
