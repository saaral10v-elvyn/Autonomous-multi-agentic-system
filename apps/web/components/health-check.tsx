"use client";
import { useEffect, useState } from "react";
type HealthState = "checking" | "online" | "offline";
export function HealthCheck() {
  const [state, setState] = useState<HealthState>("checking");
  useEffect(() => { const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"; fetch(`${api}/healthz`).then((r) => { if (!r.ok) throw new Error("health check failed"); return r.json() as Promise<{status: string}>; }).then((x) => setState(x.status === "ok" ? "online" : "offline")).catch(() => setState("offline")); }, []);
  return <span aria-live="polite" className="text-xs text-slate-400">API <span className={state === "online" ? "text-emerald-400" : state === "offline" ? "text-rose-400" : "text-amber-300"}>● {state}</span></span>;
}
