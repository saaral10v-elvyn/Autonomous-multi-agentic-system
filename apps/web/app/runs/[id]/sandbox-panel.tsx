"use client";

import { useEffect, useState } from "react";
import { api, type RunEvent, type SandboxFileEntry } from "@/lib/api";

export function SandboxPanel({ runId, events }: { runId: string; events: RunEvent[] }) {
  const [files, setFiles] = useState<SandboxFileEntry[]>([]);
  const [selected, setSelected] = useState("");
  const [content, setContent] = useState("");
  const [diff, setDiff] = useState("");
  const refreshSeq = [...events].reverse().find((event) => event.type === "agent.tool_result")?.seq ?? 0;
  useEffect(() => {
    void api.runFiles(runId).then((tree) => setFiles(tree.files)).catch(() => setFiles([]));
    void api.runDiff(runId).then((value) => setDiff(value.diff)).catch(() => setDiff(""));
  }, [runId, refreshSeq]);
  async function select(path: string) {
    setSelected(path);
    try { setContent((await api.runFileContent(runId, path)).content); }
    catch { setContent("Could not load this file."); }
  }
  const logs = events.filter((event) => event.type === "sandbox.log");
  return <section aria-label="Sandbox workspace" className="grid gap-4 rounded-xl border p-5 md:grid-cols-3">
    <div><h2 className="font-medium">Files</h2><ul className="mt-2 space-y-1 text-sm">
      {files.filter((file) => file.type === "file").map((file) => <li key={file.path}>
        <button type="button" className="underline" onClick={() => void select(file.path)}>{file.path.replace("/workspace/", "")}</button>
      </li>)}
    </ul></div>
    <div><h2 className="font-medium">{selected ? `File: ${selected}` : "Diff"}</h2>
      <pre aria-label={selected ? "File content" : "Git diff"} className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-3 text-xs">{selected ? content : diff || "No diff available."}</pre>
      {selected && <button className="mt-2 text-sm underline" onClick={() => { setSelected(""); setContent(""); }}>Show diff</button>}
    </div>
    <div><h2 className="font-medium">Live sandbox log</h2><div aria-label="Sandbox logs" className="mt-2 max-h-72 space-y-2 overflow-auto rounded bg-slate-950 p-3 font-mono text-xs text-green-200">
      {logs.length === 0 ? <p>Waiting for sandbox output.</p> : logs.map((event) => <pre key={event.seq}>[{String(event.payload.agent_id ?? "agent").slice(0, 8)} {event.payload.stream === "stderr" ? "stderr" : "stdout"}] {typeof event.payload.chunk === "string" ? event.payload.chunk : ""}</pre>)}
    </div></div>
  </section>;
}
