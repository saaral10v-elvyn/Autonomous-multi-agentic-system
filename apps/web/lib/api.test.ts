import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

describe("API validation errors", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("turns FastAPI 422 details into a readable error message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({
        detail: [{ loc: ["body", "plan", "tech_stack"], msg: "Field required", type: "missing" }],
      }),
    }));

    await expect(api.updateRunPlan("run-1", {
      product_brief: "A journal.",
      tech_stack: [],
      milestones: [{ id: "m1", title: "Start", description: "Begin." }],
      tasks: [{ id: "t1", title: "Page", description: "Build it.", depends_on: [] }],
    })).rejects.toThrow("body plan tech_stack: Field required");
  });
});
