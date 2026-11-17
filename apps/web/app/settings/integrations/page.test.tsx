import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import IntegrationsPage from "./page";

vi.mock("@/lib/api", () => ({ api: {
  integrations: vi.fn().mockResolvedValue([]),
  connectIntegration: vi.fn().mockResolvedValue({ id: "i1", kind: "github", account_label: "Work", last4: "1234", created_at: "2026-01-01" }),
  removeIntegration: vi.fn().mockResolvedValue(undefined),
} }));
afterEach(() => cleanup());

describe("IntegrationsPage", () => {
  it("connects a credential without rendering its value", async () => {
    render(<IntegrationsPage />);
    fireEvent.change(await screen.findByLabelText("Service"), { target: { value: "github" } });
    fireEvent.change(screen.getByLabelText("Account label"), { target: { value: "Work" } });
    fireEvent.change(screen.getByLabelText("Access token"), { target: { value: "ghp-test-secret-token" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByRole("status")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("ghp-test-secret-token")).toBeNull());
  });
});
