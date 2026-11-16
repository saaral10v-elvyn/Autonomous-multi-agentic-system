import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SandboxPanel } from "./sandbox-panel";

vi.mock("@/lib/api", () => ({ api: {
  runFiles: vi.fn().mockResolvedValue({ files: [{ path: "/workspace/main.py", name: "main.py", type: "file" }] }),
  runDiff: vi.fn().mockResolvedValue({ diff: "diff --git main.py" }),
  runFileContent: vi.fn().mockResolvedValue({ path: "/workspace/main.py", content: "print(1)" }),
} }));
afterEach(() => cleanup());
describe("SandboxPanel", () => {
  it("shows files, diffs, and streamed output", async () => {
    render(<SandboxPanel runId="r1" events={[{ id: "e1", run_id: "r1", seq: 1, type: "sandbox.log", payload: { agent_id: "a1", stream: "stdout", chunk: "passed" }, created_at: "2026-01-01" }]} />);
    expect(await screen.findByRole("button", { name: "main.py" })).toBeTruthy();
    expect(screen.getByLabelText("Sandbox logs").textContent).toBe("[a1 stdout] passed");
    expect(screen.getByLabelText("Git diff").textContent).toContain("diff --git");
  });
  it("loads selected file content", async () => {
    render(<SandboxPanel runId="r1" events={[]} />);
    fireEvent.click(await screen.findByRole("button", { name: "main.py" }));
    await waitFor(() => expect(screen.getByLabelText("File content").textContent).toBe("print(1)"));
  });
});
