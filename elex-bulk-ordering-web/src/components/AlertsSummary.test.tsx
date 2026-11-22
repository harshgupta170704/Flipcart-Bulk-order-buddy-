// @vitest-environment jsdom
/**
 * The dashboard's "Needs attention" rendered for real (spec FR-043): every
 * open kind with its count, so however many kinds are open the counts on
 * screen add up to the active total (SC-004); with nothing open, the resolved
 * total and "Open all alerts" still there; and a cold open that shows
 * placeholders from its very first frame, never "Could not load" (FR-049).
 */
import { useLayoutEffect } from "react";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AlertSummary } from "../lib/types";

const mocks = vi.hoisted(() => ({
  alertsSummary: vi.fn(),
  listAlerts: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

/** The counts live in one module-level store: a fresh copy per test, so each starts cold. */
let alerts: typeof import("./AlertsSummary");

beforeEach(async () => {
  vi.resetModules();
  alerts = await import("./AlertsSummary");
  mocks.listAlerts.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

/** A summary whose open total is the sum of its kinds, as the server's is. */
function summary(byKind: Record<string, number>, resolved: number): AlertSummary {
  const open = Object.values(byKind).reduce((sum, count) => sum + count, 0);
  return { open, resolved, by_severity: { info: 0, warning: open, critical: 0 }, by_kind: byKind };
}

function mount(probe?: () => void) {
  const { AlertsSummaryPanel } = alerts;
  return render(
    <MemoryRouter>
      <AlertsSummaryPanel showRefresh={false} />
      {probe ? <FirstFrame onFrame={probe} /> : null}
    </MemoryRouter>,
  );
}

/**
 * Calls `onFrame` while the first render is on the page and nothing has run
 * after it: layout effects run before any passive effect, the store's first
 * fetch among them, so this sees exactly what the first frame showed.
 */
function FirstFrame({ onFrame }: { onFrame: () => void }) {
  useLayoutEffect(() => {
    onFrame();
  }, [onFrame]);
  return null;
}

/** The "N active · M resolved all time" line, and the active total it prints. */
function totalsLine(): { text: string; active: number } {
  const line = screen.getByText(/active ·/);
  const text = line.textContent ?? "";
  return { text, active: Number(text.match(/^([\d,]+) active/)?.[1].replace(/,/g, "")) };
}

describe("Needs attention", () => {
  it("lists every open kind with its count, and the counts add up to the active total", async () => {
    // Eight kinds open, more than the six the list used to stop at; the two
    // address kinds have the fewest, so a cut would have hidden them first.
    mocks.alertsSummary.mockResolvedValue(
      summary(
        {
          delivery_overdue: 9,
          order_unconfirmed: 7,
          order_failed: 6,
          over_target: 5,
          gst_mismatch: 4,
          product_invoice_missing: 3,
          address_mismatch: 2,
          address_change: 1,
        },
        40,
      ),
    );
    mount();

    const byType = await screen.findByRole("region", { name: "Open alerts by type" });
    const rows = within(byType).getAllByRole("listitem");
    expect(rows).toHaveLength(8);
    const counts = rows.map((row) => Number(row.lastElementChild?.textContent?.replace(/,/g, "")));
    const { text, active } = totalsLine();
    expect(text).toBe("37 active · 40 resolved all time");
    expect(counts.reduce((sum, count) => sum + count, 0)).toBe(active);
    within(byType).getByRole("link", { name: "Order unit address mismatch" });
    within(byType).getByRole("link", { name: "Order unit address change" });
    expect(screen.queryByText(/more types/)).toBeNull();
  });

  it("with nothing open, still shows the resolved total and the way into the queue", async () => {
    mocks.alertsSummary.mockResolvedValue(summary({}, 12));
    mount();

    expect(await screen.findByText("Nothing needs attention")).toBeTruthy();
    expect(totalsLine().text).toBe("0 active · 12 resolved all time");
    expect(screen.getByRole("link", { name: "Open all alerts" }).getAttribute("href")).toBe("/alerts");
  });

  it("shows placeholders from the first frame of a cold open, never the error", async () => {
    let answer: (value: AlertSummary) => void = () => undefined;
    mocks.alertsSummary.mockReturnValue(
      new Promise<AlertSummary>((resolve) => {
        answer = resolve;
      }),
    );
    let first: { placeholders: boolean; error: boolean } | null = null;
    mount(() => {
      first ??= {
        placeholders: screen.queryByRole("status", { name: "Loading" }) !== null,
        error: screen.queryByText("Could not load the alerts queue") !== null,
      };
    });

    expect(first).toEqual({ placeholders: true, error: false });
    // Still placeholders while the counts are on their way, then the counts.
    expect(screen.getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(mocks.alertsSummary).toHaveBeenCalledTimes(1);
    await act(async () => answer(summary({ delivery_overdue: 2 }, 3)));
    expect(totalsLine().text).toBe("2 active · 3 resolved all time");
  });
});
