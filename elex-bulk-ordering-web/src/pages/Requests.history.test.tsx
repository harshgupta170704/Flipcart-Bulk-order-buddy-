// @vitest-environment jsdom
/**
 * A request's history on the Requests page (spec C-4's status log): every
 * step, oldest first, folded away until asked for; nothing extra for a
 * request that has only been opened.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { UnitRequestStep } from "../lib/types";
import { RequestHistory } from "./Requests";

afterEach(cleanup);

const STEPS: UnitRequestStep[] = [
  { status: "open", actor: "alice@test.com", at: "2026-10-26T09:00:00", note: null },
  { status: "done", actor: "bob@test.com", at: "2026-10-26T10:00:00", note: "cancelled on Flipkart" },
  { status: "open", actor: "alice@test.com", at: "2026-10-26T11:00:00", note: null },
  { status: "dropped", actor: "bob@test.com", at: "2026-10-26T12:00:00", note: "kept after all" },
];

describe("RequestHistory", () => {
  it("lists every step in words, the first close included", () => {
    render(<RequestHistory steps={STEPS} />);
    fireEvent.click(screen.getByText("History (4 steps)"));
    const items = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
    expect(items[0]).toMatch(/^Opened by alice@test.com/);
    expect(items[1]).toMatch(/^Marked done by bob@test.com/);
    expect(items[1]).toContain("cancelled on Flipkart");
    expect(items[2]).toMatch(/^Opened again by alice@test.com/);
    expect(items[3]).toMatch(/^Dropped by bob@test.com/);
  });

  it("adds nothing for a request that has only been opened", () => {
    const { container } = render(<RequestHistory steps={STEPS.slice(0, 1)} />);
    expect(container.textContent).toBe("");
  });
});
