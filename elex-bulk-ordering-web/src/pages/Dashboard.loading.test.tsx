// @vitest-environment jsdom
/**
 * The dashboard while it loads (spec FR-049): every panel shows placeholders
 * in place of its figures — the totals, Orders by status, Recent jobs, To be
 * delivered, Out for delivery, the changes list and Needs attention — from the
 * very first frame, and none says it could not load before anything has had
 * the chance to.
 */
import { useLayoutEffect } from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getStats: vi.fn(),
  listJobs: vi.fn(),
  getOutForDelivery: vi.fn(),
  getToBeDelivered: vi.fn(),
  alertsSummary: vi.fn(),
  listAlerts: vi.fn(),
  startSyncJob: vi.fn(),
  waitForJob: vi.fn(),
  // The Account logins card; left loading in these tests.
  getLoginHealth: vi.fn(() => new Promise(() => undefined)),
  listChanges: vi.fn(),
  markChangesSeen: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  getFilterOptions: () => Promise.resolve({ statuses: [], subscriptions: [], tags: [], total: 0, untagged: 0, domains: [] }),
  getBalanceTotals: () => Promise.resolve({ supercoins_balance: 0, gift_voucher_balance: 0, accounts_read: 0, supercoins_orders_delivered: 0 }),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import Dashboard from "./Dashboard";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

/** The cards that load figures of their own, by their titles while loading. */
const CARDS = [
  "Orders by status",
  "Recent jobs",
  "To be delivered",
  "Out for delivery",
  "Changes since you last looked",
  "Needs attention",
];

/** Which panels show their placeholders right now — every one, while loading. */
function placeholders(): Record<string, boolean> {
  const shown: Record<string, boolean> = {};
  // The totals are grey tiles, hidden from screen readers, in a busy region.
  const totals = screen.queryByRole("region", { name: "Totals" });
  const tiles = Array.from(totals?.children ?? []);
  shown.Totals =
    totals?.getAttribute("aria-busy") === "true" &&
    tiles.length > 0 &&
    tiles.every((tile) => tile.getAttribute("aria-hidden") === "true");
  for (const title of CARDS) {
    const card = screen.getByRole("heading", { level: 2, name: title }).closest("section") as HTMLElement;
    shown[title] = within(card).queryByRole("status", { name: "Loading" }) !== null;
  }
  return shown;
}

const EVERY_PANEL = Object.fromEntries(["Totals", ...CARDS].map((panel) => [panel, true]));

/** Records the first frame: layout effects run before any panel's first fetch. */
function FirstFrame({ onFrame }: { onFrame: () => void }) {
  useLayoutEffect(() => {
    onFrame();
  }, [onFrame]);
  return null;
}

describe("Dashboard while loading", () => {
  it("shows placeholders in every panel from the first frame until the figures land", () => {
    // Nothing ever answers: the page stays as it is on a slow first load.
    for (const fetcher of Object.values(mocks)) fetcher.mockReturnValue(new Promise(() => undefined));
    let first: { shown: Record<string, boolean>; failed: boolean } | null = null;

    render(
      <MemoryRouter>
        <Dashboard />
        <FirstFrame
          onFrame={() => {
            first ??= { shown: placeholders(), failed: screen.queryByText(/Could not load/) !== null };
          }}
        />
      </MemoryRouter>,
    );

    expect(first).toEqual({ shown: EVERY_PANEL, failed: false });
    // And still, once every panel has asked for its figures.
    expect(placeholders()).toEqual(EVERY_PANEL);
    expect(screen.queryByText(/Could not load/)).toBeNull();
    expect(mocks.getToBeDelivered).toHaveBeenCalled();
    expect(mocks.alertsSummary).toHaveBeenCalled();
  });
});
