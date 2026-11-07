"use client";

import { FormEvent, useState } from "react";
import { api } from "@/lib/api";

type AuthFormProps = { mode: "login" | "signup" };

export function AuthForm({ mode }: AuthFormProps) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    const data = new FormData(event.currentTarget);
    try {
      if (mode === "signup") await api.signup(String(data.get("email")), String(data.get("password")));
      else await api.login(String(data.get("email")), String(data.get("password")));
      window.location.assign("/settings/providers");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Authentication failed.");
    } finally {
      setBusy(false);
    }
  }

  return <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
    <a className="mb-8 text-xl font-bold" href="/">Forge<span className="text-emerald-400">.</span></a>
    <h1 className="text-3xl font-semibold">{mode === "signup" ? "Create your account" : "Welcome back"}</h1>
    <form className="mt-8 grid gap-5" onSubmit={submit}>
      <label className="grid gap-2 text-sm">Email<input className="rounded-md border border-slate-700 bg-slate-900 px-3 py-2" type="email" name="email" required autoComplete="email" /></label>
      <label className="grid gap-2 text-sm">Password<input className="rounded-md border border-slate-700 bg-slate-900 px-3 py-2" type="password" name="password" minLength={8} required autoComplete={mode === "signup" ? "new-password" : "current-password"} /></label>
      {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
      <button className="rounded-md bg-emerald-400 px-4 py-2 font-semibold text-slate-950 disabled:opacity-60" disabled={busy}>{busy ? "Please wait…" : mode === "signup" ? "Sign up" : "Log in"}</button>
    </form>
    <p className="mt-6 text-sm text-slate-400">{mode === "signup" ? "Already have an account? " : "New to Forge? "}<a className="text-emerald-300" href={mode === "signup" ? "/login" : "/signup"}>{mode === "signup" ? "Log in" : "Sign up"}</a></p>
  </main>;
}
