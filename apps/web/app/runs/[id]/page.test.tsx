import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type RunEvent } from "@/lib/api";
import RunPage from "./page";

const stream = vi.hoisted(() => ({
  events: [] as Array<{
    id: string;
    run_id: string;
    seq: number;
    type: string;
    payload: Record<string, unknown>;
    created_at: string;
  }>,
}));

vi.mock("next/navigation", () => ({ useParams: () => ({ id: "run-1" }) }));
vi.mock("@/hooks/use-run-stream", () => ({
  useRunStream: () => ({ events: stream.events as RunEvent[], connected: true }),
}));

describe("run clarification and plan interface", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    stream.events = [];
    vi.spyOn(api, "run").mockResolvedValue({
      id: "run-1",
      project_id: "project-1",
      idea: "Build a journal",
      autonomy_level: "step_by_step",
      budget_usd: "25.00",
      status: "AWAITING_ANSWERS",
      created_at: "2026-11-08T00:00:00Z",
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders the questions form and submits a decide-for-me answer", async () => {
    stream.events = [{
      id: "event-1",
      run_id: "run-1",
      seq: 3,
      type: "question.asked",
      payload: {
        questions: [{
          id: "audience",
          prompt: "Who is this for?",
          type: "free_text",
          options: [],
          allow_decide_for_me: true,
        }],
      },
      created_at: "2026-11-08T00:00:00Z",
    }];
    vi.spyOn(api, "submitRunAnswers").mockResolvedValue({
      id: "run-1",
      project_id: "project-1",
      idea: "Build a journal",
      autonomy_level: "step_by_step",
      budget_usd: "25.00",
      status: "PLANNING",
      created_at: "2026-11-08T00:00:00Z",
    });

    render(<RunPage />);
    expect(await screen.findByText("Who is this for?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Decide for me" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit answers" }));
    await waitFor(() => expect(api.submitRunAnswers).toHaveBeenCalledWith("run-1", { audience: "decide_for_me" }));
  });

  it("submits single-choice, multi-choice, and regular free-text answers", async () => {
    stream.events = [{
      id: "questions-1",
      run_id: "run-1",
      seq: 3,
      type: "question.asked",
      payload: {
        questions: [
          {
            id: "audience", prompt: "Who is this for?", type: "single_choice",
            options: [{ id: "teams", label: "Teams" }, { id: "families", label: "Families" }],
            allow_decide_for_me: false,
          },
          {
            id: "features", prompt: "Which features?", type: "multi_choice",
            options: [{ id: "offline", label: "Offline" }, { id: "sharing", label: "Sharing" }],
            allow_decide_for_me: false,
          },
          {
            id: "notes", prompt: "Any other details?", type: "free_text",
            options: [], allow_decide_for_me: false,
          },
        ],
      },
      created_at: "2026-11-08T00:00:00Z",
    }];
    vi.spyOn(api, "submitRunAnswers").mockResolvedValue({
      id: "run-1", project_id: "project-1", idea: "Build a journal",
      autonomy_level: "step_by_step", budget_usd: "25.00", status: "PLANNING",
      created_at: "2026-11-08T00:00:00Z",
    });

    render(<RunPage />);
    expect((await screen.findByLabelText("Run state")).textContent).toContain("AWAITING_ANSWERS");
    fireEvent.change(screen.getByLabelText("Who is this for?"), { target: { value: "teams" } });
    fireEvent.click(screen.getByLabelText("Offline"));
    fireEvent.change(screen.getByLabelText("Any other details?"), {
      target: { value: "Keep the design calm." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit answers" }));

    await waitFor(() => expect(api.submitRunAnswers).toHaveBeenCalledWith("run-1", {
      audience: "teams",
      features: ["offline"],
      notes: "Keep the design calm.",
    }));
  });

  it("shows the plan editor and supports saving and approving the plan", async () => {
    stream.events = [
      {
        id: "state-1", run_id: "run-1", seq: 4, type: "run.state_changed",
        payload: { status: "AWAITING_PLAN_APPROVAL" }, created_at: "2026-11-08T00:00:00Z",
      },
      {
        id: "plan-1", run_id: "run-1", seq: 5, type: "plan.created",
        payload: { plan: {
          product_brief: "A journal.",
          tech_stack: ["Next.js"],
          milestones: [{ id: "m1", title: "Start", description: "Create the app." }],
          tasks: [{ id: "t1", title: "Page", description: "Make a page.", depends_on: [] }],
        } },
        created_at: "2026-11-08T00:00:00Z",
      },
    ];
    vi.spyOn(api, "updateRunPlan").mockResolvedValue({
      product_brief: "An improved journal.",
      tech_stack: ["Next.js"],
      milestones: [{ id: "m1", title: "Start", description: "Create the app." }],
      tasks: [{ id: "t1", title: "Page", description: "Make a page.", depends_on: [] }],
    });
    vi.spyOn(api, "approveRunPlan").mockResolvedValue({
      id: "run-1", project_id: "project-1", idea: "Build a journal", autonomy_level: "step_by_step",
      budget_usd: "25.00", status: "EXECUTING", created_at: "2026-11-08T00:00:00Z",
    });

    render(<RunPage />);
    expect(await screen.findByText("A journal.")).toBeTruthy();
    const editor = screen.getByLabelText("Edit plan JSON");
    const updatedPlan = {
      product_brief: "An improved journal.",
      tech_stack: ["Next.js"],
      milestones: [{ id: "m1", title: "Start", description: "Create the app." }],
      tasks: [{ id: "t1", title: "Page", description: "Make a page.", depends_on: [] }],
    };
    fireEvent.change(editor, { target: { value: JSON.stringify(updatedPlan) } });
    fireEvent.click(screen.getByRole("button", { name: "Save edits" }));
    await waitFor(() => expect(api.updateRunPlan).toHaveBeenCalledWith("run-1", updatedPlan));
    fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));
    await waitFor(() => expect(api.approveRunPlan).toHaveBeenCalledWith("run-1"));
  });

  it("shows a clear message when the plan editor contains invalid JSON", async () => {
    stream.events = [
      {
        id: "state-1", run_id: "run-1", seq: 4, type: "run.state_changed",
        payload: { status: "AWAITING_PLAN_APPROVAL" }, created_at: "2026-11-08T00:00:00Z",
      },
      {
        id: "plan-1", run_id: "run-1", seq: 5, type: "plan.created",
        payload: { plan: {
          product_brief: "A journal.", tech_stack: ["Next.js"],
          milestones: [{ id: "m1", title: "Start", description: "Create the app." }],
          tasks: [{ id: "t1", title: "Page", description: "Make a page.", depends_on: [] }],
        } },
        created_at: "2026-11-08T00:00:00Z",
      },
    ];
    render(<RunPage />);
    const editor = await screen.findByLabelText("Edit plan JSON");
    fireEvent.change(editor, { target: { value: "{" } });
    fireEvent.click(screen.getByRole("button", { name: "Save edits" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Plan must be valid JSON.");
  });

  it("shows server validation and conflict errors to the user", async () => {
    stream.events = [{
      id: "questions-1", run_id: "run-1", seq: 3, type: "question.asked",
      payload: { questions: [{
        id: "audience", prompt: "Who is this for?", type: "free_text", options: [],
        allow_decide_for_me: false,
      }] },
      created_at: "2026-11-08T00:00:00Z",
    }];
    vi.spyOn(api, "submitRunAnswers").mockRejectedValue(
      new Error("This run is not waiting for answers."),
    );
    render(<RunPage />);
    fireEvent.change(await screen.findByLabelText("Who is this for?"), {
      target: { value: "A team" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit answers" }));
    expect((await screen.findByRole("alert")).textContent).toContain(
      "This run is not waiting for answers.",
    );
  });
});
