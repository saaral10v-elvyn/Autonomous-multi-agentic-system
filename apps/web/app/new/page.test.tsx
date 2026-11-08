import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import NewRunPage, { validateRunForm } from "./page";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

describe("new run form", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(api, "createRun").mockResolvedValue({
      id: "run-1",
      project_id: "project-1",
      idea: "Build a journal",
      autonomy_level: "milestones",
      budget_usd: "25.00",
      status: "CREATED",
      created_at: "2026-11-08T00:00:00Z",
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("rejects an empty idea", () => {
    render(<NewRunPage />);
    fireEvent.submit(screen.getByRole("button", { name: "Create run" }).closest("form")!);
    expect(screen.getByRole("alert").textContent).toMatch(/enter an idea/i);
    expect(api.createRun).not.toHaveBeenCalled();
  });

  it("rejects an autonomy level outside the supported choices", () => {
    expect(validateRunForm("Build a journal", "hands_off", "10")).toMatch(/valid autonomy/i);
    render(<NewRunPage />);
    expect(screen.getAllByRole("radio").map((radio) => (radio as HTMLInputElement).value)).toEqual([
      "step_by_step",
      "milestones",
      "autopilot",
    ]);
  });

  it("rejects a negative budget", () => {
    render(<NewRunPage />);
    fireEvent.change(screen.getByLabelText(/your idea/i), { target: { value: "Build a journal" } });
    fireEvent.change(screen.getByLabelText(/budget cap/i), { target: { value: "-1" } });
    fireEvent.submit(screen.getByRole("button", { name: "Create run" }).closest("form")!);
    expect(screen.getByRole("alert").textContent).toMatch(/budget of zero or more/i);
    expect(api.createRun).not.toHaveBeenCalled();
  });
});
