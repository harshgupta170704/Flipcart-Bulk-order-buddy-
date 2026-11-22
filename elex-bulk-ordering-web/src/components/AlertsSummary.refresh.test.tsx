// @vitest-environment jsdom
/**
 * "Needs attention" while its counts reload (spec FR-049; audit of 27 Oct
 * 2026) — on the dashboard's Refresh, which calls `refreshAlertSummary()`:
 * placeholders in place of the counts until the new counts land, then
 * placeholders in place of the latest rows until the rows that go with those
 * counts land, never the counts or rows from before as if they were the
 * Refresh's answer. A reload that fails brings the last counts back, saying
 * so; while the next try is on its way, that note goes with the counts.
 */
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Alert as AlertRow, AlertSummary } from "../lib/types";

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
  // Each landing of the counts is told apart by its time: keep them apart.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-27T09:00:00Z"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
});

function counts(open: number, resolved: number): AlertSummary {
  return { open, resolved, by_severity: { info: 0, warning: open, critical: 0 }, by_kind: { delivery_overdue: open } };
}

function row(id: number, message: string): AlertRow {
  return {
    id,
    key: `delivery_overdue:order:${id}`,
    kind: "delivery_overdue",
    severity: "warning",
    message,
    account_id: 2,
    order_id: id,
    details: null,
    resolved: false,
    created_at: "2026-10-27T08:00:00Z",
    updated_at: "2026-10-27T08:00:00Z",
    resolved_at: null,
  };
}

/** A reply the test answers when it chooses. */
function later<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: Error) => void = () => undefined;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function mount() {
  const { AlertsSummaryPanel } = alerts;
  return render(
    <MemoryRouter>
      <AlertsSummaryPanel showRefresh={false} />
    </MemoryRouter>,
  );
}

function panel(): HTMLElement {
  return screen.getByRole("heading", { level: 2, name: "Needs attention" }).closest("section") as HTMLElement;
}

/** The "N active · M resolved all time" line, whole; null when it is not on screen. */
function countsLine(): string | null {
  return screen.queryByText(/active ·/)?.textContent ?? null;
}

/** Shows the panel with its first counts and latest rows, as a cold open leaves it. */
async function firstLoad() {
  mocks.alertsSummary.mockResolvedValueOnce(counts(2, 3));
  mocks.listAlerts.mockResolvedValueOnce([row(1, "Order OD1 is 3 days overdue")]);
  mount();
  await screen.findByText("Order OD1 is 3 days overdue");
  expect(countsLine()).toBe("2 active · 3 resolved all time");
}

describe("Needs attention while its counts reload", () => {
  it("shows placeholders until the new counts land, then until the rows that go with them land", async () => {
    await firstLoad();
    const reply = later<AlertSummary>();
    const rows = later<AlertRow[]>();
    mocks.alertsSummary.mockReturnValueOnce(reply.promise);
    mocks.listAlerts.mockReturnValueOnce(rows.promise);

    act(() => alerts.refreshAlertSummary());

    expect(within(panel()).getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(countsLine()).toBeNull();
    expect(screen.queryByText("Order OD1 is 3 days overdue")).toBeNull();
    expect(screen.queryByRole("region", { name: "Open alerts by type" })).toBeNull();

    vi.setSystemTime(new Date("2026-10-27T09:01:00Z"));
    await act(async () => reply.resolve(counts(4, 5)));

    expect(countsLine()).toBe("4 active · 5 resolved all time");
    const latest = screen.getByRole("region", { name: "Latest open alerts" });
    expect(within(latest).getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(within(latest).queryByText("Order OD1 is 3 days overdue")).toBeNull();
    expect(mocks.listAlerts).toHaveBeenCalledTimes(2);

    await act(async () => rows.resolve([row(2, "Order OD2 is 5 days overdue")]));

    expect(within(latest).getByText("Order OD2 is 5 days overdue")).toBeTruthy();
    expect(within(panel()).queryByRole("status", { name: "Loading" })).toBeNull();
  });

  it("brings the last counts back, saying so, when the reload fails — and not while the next try is on its way", async () => {
    await firstLoad();
    const failed = later<AlertSummary>();
    mocks.alertsSummary.mockReturnValueOnce(failed.promise);

    act(() => alerts.refreshAlertSummary());
    expect(countsLine()).toBeNull();
    await act(async () => failed.reject(new Error("Server unreachable")));

    expect(countsLine()).toBe("2 active · 3 resolved all time");
    expect(screen.getByText("Order OD1 is 3 days overdue")).toBeTruthy();
    expect(screen.getByText(/^Showing the counts that last loaded/).textContent).toMatch(/the refresh failed: Server unreachable$/);
    // The rows went with counts that did not change: not asked for again.
    expect(mocks.listAlerts).toHaveBeenCalledTimes(1);

    const retry = later<AlertSummary>();
    mocks.alertsSummary.mockReturnValueOnce(retry.promise);
    act(() => alerts.refreshAlertSummary());

    expect(within(panel()).getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(countsLine()).toBeNull();
    expect(screen.queryByText(/^Showing the counts that last loaded/)).toBeNull();
  });
});
