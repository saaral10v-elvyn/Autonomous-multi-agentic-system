import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Home from "./page";
describe("home page", () => { it("introduces Forge and the workflow", () => { render(<Home />); expect(screen.getByRole("heading", { name: /from first thought to live app/i })).toBeTruthy(); expect(screen.getByText(/share your idea/i)).toBeTruthy(); }); });
