"use client";

import { useEffect, useState } from "react";
import { api, type RunEvent } from "@/lib/api";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function useRunStream(runId: string) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let lastSeq = 0;
    const seen = new Set<number>();

    const add = (event: RunEvent) => {
      if (stopped || event.run_id !== runId || seen.has(event.seq)) return;
      seen.add(event.seq);
      lastSeq = Math.max(lastSeq, event.seq);
      setEvents((current) => [...current, event].sort((a, b) => a.seq - b.seq));
    };

    const connect = async () => {
      try {
        const replay = await api.runEvents(runId, lastSeq);
        replay.forEach(add);
      } catch {
        if (stopped) return;
      }
      if (stopped) return;
      const url = new URL(`/ws/runs/${runId}?after_seq=${lastSeq}`, API_URL);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(url);
      socket.onopen = () => setConnected(true);
      socket.onmessage = (message: MessageEvent<string>) => {
        try {
          const parsed: unknown = JSON.parse(message.data);
          if (typeof parsed === "object" && parsed !== null && "seq" in parsed && "run_id" in parsed) {
            add(parsed as RunEvent);
          }
        } catch {
          // Ignore malformed server frames and keep the stream alive.
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (!stopped) retryTimer = setTimeout(() => void connect(), 1000);
      };
      socket.onerror = () => socket?.close();
    };

    void connect();
    return () => {
      stopped = true;
      if (retryTimer) clearTimeout(retryTimer);
      socket?.close();
    };
  }, [runId]);

  return { events, connected };
}
