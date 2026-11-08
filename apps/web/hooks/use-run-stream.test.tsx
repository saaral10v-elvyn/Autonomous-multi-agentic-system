import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type RunEvent } from "@/lib/api";
import { useRunStream } from "./use-run-stream";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  readonly url: string;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(url: string | URL) {
    this.url = String(url);
    MockWebSocket.instances.push(this);
  }

  close(): void {
    this.onclose?.(new CloseEvent("close"));
  }

  emit(event: RunEvent): void {
    this.onmessage?.({ data: JSON.stringify(event) } as MessageEvent<string>);
  }
}

function event(seq: number): RunEvent {
  return {
    id: `event-${seq}`,
    run_id: "run-1",
    seq,
    type: "run.state_changed",
    payload: { seq },
    created_at: "2026-11-08T00:00:00Z",
  };
}

describe("useRunStream", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    MockWebSocket.instances = [];
  });

  it("reconnects from the last sequence and ignores duplicate replay events", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
    const runEvents = vi.spyOn(api, "runEvents").mockImplementation(async (_id, afterSeq) =>
      afterSeq === 0 ? [event(1)] : [event(2), event(3)],
    );
    const { result } = renderHook(() => useRunStream("run-1"));

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    expect(MockWebSocket.instances[0]?.url).toContain("after_seq=1");
    act(() => MockWebSocket.instances[0]?.emit(event(2)));
    act(() => MockWebSocket.instances[0]?.close());

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(2), { timeout: 3000 });
    expect(runEvents.mock.calls.map((call) => call[1])).toEqual([0, 2]);
    expect(MockWebSocket.instances[1]?.url).toContain("after_seq=3");
    expect(result.current.events.map((item) => item.seq)).toEqual([1, 2, 3]);
    act(() => MockWebSocket.instances[1]?.emit(event(3)));
    expect(result.current.events.map((item) => item.seq)).toEqual([1, 2, 3]);
  });
});
