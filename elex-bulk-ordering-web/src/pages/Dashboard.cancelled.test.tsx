// @vitest-environment jsdom
/**
 * The dashboard's order totals: "Failed" counts only runs that stopped before
 * anything reached Flipkart, and a tile of its own — and a bar of its own in
 * "Orders by status" — counts the orders Flipkart took and cancelled later.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Stats } from "../lib/types";

const mocks = vi.hoisted(() => ({
  getStats: vi.fn(),
  listJobs: vi.fn(),
  getOutForDelivery: vi.fn(),
  getToBeDelivered: vi.fn(),
  alertsSummary: vi.fn(),
  listAlerts: vi.fn(),
  startSyncJob: vi.fn(),
  waitForJob: vi.fn(),
  // The "Changes since you last looked" panel beside the totals.
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
  vi.clearAllMocks();
});

const STATS: Stats = {
  accounts: 4,
  sessions_valid: 2,
  coupons_applied: 1,
  orders_placed: 5,
  orders_over_target: 1,
  orders_failed: 2,
  orders_cancelled: 3,
  orders_cancelled_by_user: 1,
  orders_unconfirmed: 0,
  jobs_running: 0,
};

/** The big number on the tile labelled `label`. */
function tile(label: string): string {
  const name = screen.getAllByText(label).find((el) => el.className.includes("uppercase"));
  return name?.parentElement?.children[1]?.textContent ?? "";
}

describe("Dashboard order totals", () => {
  it("counts Flipkart's cancellations apart from the run's own failures", async () => {
    mocks.getStats.mockResolvedValue(STATS);
    mocks.listJobs.mockResolvedValue([]);
    mocks.getOutForDelivery.mockResolvedValue({ total: 0, units: [], account_ids: [], order_ids: [], last_read_back: null });
    mocks.getToBeDelivered.mockResolvedValue({ total_units: 0, total_orders: 0, models: [], older_than_days: null });
    mocks.alertsSummary.mockResolvedValue({ open: 0, resolved: 0, by_severity: {}, by_kind: {} });
    mocks.listAlerts.mockResolvedValue([]);
    mocks.listChanges.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      size: 20,
      pages: 1,
      by_kind: {},
      since: "2026-10-19T09:00:00",
      seen_at: null,
      capped: false,
      as_of: "2026-10-26T09:00:00",
    });

    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Stopped before anything was sent to Flipkart")).toBeTruthy();
    expect(tile("Failed")).toBe("2");
    expect(tile("Cancelled by Flipkart")).toBe("3");
    expect(
      screen.getByText(
        "Placed on Flipkart, then cancelled there, or never placed because the payment failed — the reason is on each order · 1 more cancelled at the account's request",
      ),
    ).toBeTruthy();

    // The chart has a bar for each, and its total still covers every order.
    const chart = screen.getByRole("img", { name: /Orders by status/ });
    const description = chart.querySelector("desc")?.textContent ?? "";
    expect(description).toContain("Failed 2");
    expect(description).toContain("Cancelled by Flipkart 3");
    expect(description).toContain("Cancelled by user 1");
    expect(description).toContain("12 orders in total");
  });
});
