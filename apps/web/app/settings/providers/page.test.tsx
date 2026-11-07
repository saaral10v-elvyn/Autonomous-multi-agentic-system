import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ProvidersPage from "./page";

describe("provider settings", () => {
  afterEach(() => vi.restoreAllMocks());

  it("clears the write-only key field and only shows last four after saving", async () => {
    const secret = "sk-known-ui-secret-7Qz";
    const saved = {
      id: "key-1",
      provider: "openai",
      label: "Personal",
      last4: "7Qz",
      created_at: "2026-11-07T00:00:00Z",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      if (init?.method === "POST") return new Response(JSON.stringify(saved), { status: 201 });
      return new Response(JSON.stringify([saved]), { status: 200 });
    });
    render(<ProvidersPage />);

    await screen.findByText(/no provider keys saved yet/i);
    fireEvent.change(screen.getByLabelText(/label/i), { target: { value: "Personal" } });
    fireEvent.change(screen.getByLabelText(/api key/i), { target: { value: secret } });
    fireEvent.click(screen.getByRole("button", { name: /save provider key/i }));

    await screen.findByRole("status");
    await waitFor(() => expect((screen.getByLabelText(/api key/i) as HTMLInputElement).value).toBe(""));
    expect(screen.getByText(/key ending in 7qz/i)).toBeTruthy();
    expect(document.body.textContent).not.toContain(secret);
    expect(fetchMock).toHaveBeenCalled();
  });
});
