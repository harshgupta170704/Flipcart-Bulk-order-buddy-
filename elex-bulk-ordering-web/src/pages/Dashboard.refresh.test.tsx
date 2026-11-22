// @vitest-environment jsdom
/**
 * The dashboard during its Refresh (spec FR-048, FR-049; audit of 27 Oct
 * 2026): from the moment the page's Refresh — or the Retry after a failed
 * first load — is pressed, every panel with figures shows its placeholders in
 * their place: the totals, Orders by status, Recent jobs, To be delivered,
 * Out for delivery and Needs attention. Each keeps them until its own reply
 * lands and then shows the new figures; none shows the figures from before
 * the Refresh as if they were its answer. The five-second poll, which exists
 * for job progress, is not the user's Refresh: the figures stay through it.
 */
import { Profiler } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  Alert as AlertRow,
  AlertSummary,
  ChangesPage,
  Job,
  OutForDelivery,
  OutForDeliveryUnit,
  Stats,
  ToBeDelivered,
} from "../lib/types";

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

/** The alert counts and the changes list live in module-level stores: a fresh copy per test, so each starts cold. */
let Dashboard: typeof import("./Dashboard").default;

/** While `recording`, what every frame the page draws shows of the first load's figures. */
let frames: Record<string, boolean>[] = [];
let recording = false;

beforeEach(async () => {
  vi.resetModules();
  Dashboard = (await import("./Dashboard")).default;
  frames = [];
  recording = false;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
});

/* --- what the server answers ------------------------------------------------ */

const STATS: Stats = {
  accounts: 4,
  sessions_valid: 2,
  coupons_applied: 1,
  orders_placed: 5,
  orders_over_target: 1,
  orders_failed: 2,
  orders_cancelled: 0,
  orders_cancelled_by_user: 0,
  orders_unconfirmed: 0,
  jobs_running: 0,
};

const JOB: Job = {
  id: 7,
  type: "order",
  status: "completed",
  progress: 100,
  total_items: 3,
  done_items: 3,
  failed_items: 0,
  skipped_items: 0,
  unconfirmed_items: 0,
  cancel_requested: false,
  payload: null,
  started_at: "2026-10-27T09:00:00",
  finished_at: "2026-10-27T09:05:00",
};

function toBeDelivered(units: number, model: string): ToBeDelivered {
  return {
    total_units: units,
    total_orders: units,
    older_than_days: null,
    models: [{ brand: "vivo", model, units, orders: units, oldest_ordered_at: "2026-10-01T10:00:00" }],
  };
}

function outForDelivery(code: string): OutForDelivery {
  const unit: OutForDeliveryUnit = {
    unit_pk: 31,
    order_row_id: 14,
    order_id: "OD338980309549917100",
    unit_id: "338980309549917100000",
    position: 1,
    account_id: 2,
    account_email: "two@test.com",
    model: "Samsung Galaxy M36",
    color: "Blue",
    platform_status: "Out For Delivery",
    delivery_otp: code,
    delivery_date: null,
    delivery_address: null,
    delivery_pincode: "226013",
    tracking_id: null,
    synced_at: "2026-10-27T04:30:00",
  };
  return { total: 1, units: [unit], account_ids: [2], order_ids: [unit.order_id ?? ""], last_read_back: unit.synced_at };
}

function counts(open: number, resolved: number): AlertSummary {
  return { open, resolved, by_severity: { info: 0, warning: open, critical: 0 }, by_kind: { delivery_overdue: open } };
}

function latestAlert(id: number, message: string): AlertRow {
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
    created_at: "2026-10-27T08:00:00",
    updated_at: "2026-10-27T08:00:00",
    resolved_at: null,
  };
}

const NO_CHANGES: ChangesPage = {
  items: [],
  total: 0,
  page: 1,
  size: 20,
  pages: 1,
  by_kind: {},
  since: "2026-10-20T09:00:00",
  seen_at: null,
  capped: false,
  as_of: "2026-10-27T09:00:00",
};

/** A reply the test answers when it chooses. */
function later<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}

/**
 * Needs attention tells one landing of the alert counts from the next by the
 * time it landed: a clock that moves only when the test moves it keeps the
 * Refresh's counts apart from the first load's, however fast the test runs.
 */
function stopTheClock() {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-27T09:00:00Z"));
}

/* --- what the page shows ---------------------------------------------------- */

/** The panels with figures of their own, by the start of their card titles. */
const CARDS = ["Orders by status", "Recent jobs", "To be delivered", "Out for delivery", "Needs attention"];

function card(title: string): HTMLElement {
  return screen.getByRole("heading", { level: 2, name: new RegExp(`^${title}`) }).closest("section") as HTMLElement;
}

/** Which panels show placeholders right now. */
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
    shown[title] = within(card(title)).queryByRole("status", { name: "Loading" }) !== null;
  }
  return shown;
}

const PANELS = ["Totals", ...CARDS];
const EVERY_PANEL = Object.fromEntries(PANELS.map((panel) => [panel, true]));
const NO_PANEL = Object.fromEntries(PANELS.map((panel) => [panel, false]));

/** The big number on the tile labelled `label`. */
function tile(label: string): string {
  const name = screen.getAllByText(label).find((el) => el.className.includes("uppercase"));
  return name?.parentElement?.children[1]?.textContent ?? "";
}

/** Every figure the first load put on the page, and whether each is on screen now. */
function firstFigures(): Record<string, boolean> {
  return {
    tiles: screen.queryByText("Loaded into the suite") !== null,
    chart: screen.queryByRole("img", { name: /Orders by status/ }) !== null,
    chartTotal: screen.queryByText("8 orders placed by runs or tracked by upload so far.") !== null,
    job: screen.queryByText("#7") !== null,
    toBeDelivered: screen.queryByText("To be delivered — 5 order units across 1 model") !== null,
    model: screen.queryByText("vivo T5x 5G") !== null,
    code: screen.queryByText("482713") !== null,
    outCount: screen.queryByText("Out for delivery — 1 unit") !== null,
    // The counts line is split over spans: read it whole.
    alertCounts: screen.queryByText(/active ·/)?.textContent === "2 active · 3 resolved all time",
    latest: screen.queryByText("Order OD1 is 3 days overdue") !== null,
  };
}

const FIGURES = ["tiles", "chart", "chartTotal", "job", "toBeDelivered", "model", "code", "outCount", "alertCounts", "latest"];
const EVERY_FIGURE = Object.fromEntries(FIGURES.map((figure) => [figure, true]));
const NO_FIGURE = Object.fromEntries(FIGURES.map((figure) => [figure, false]));

function mount() {
  return render(
    <MemoryRouter>
      <Profiler
        id="dashboard"
        onRender={() => {
          if (recording) frames.push(firstFigures());
        }}
      >
        <Dashboard />
      </Profiler>
    </MemoryRouter>,
  );
}

/** Presses `name`, keeping what every frame drawn meanwhile showed. */
function press(name: string) {
  frames = [];
  recording = true;
  fireEvent.click(screen.getByRole("button", { name }));
  recording = false;
}

/** Every panel answers the first load at once. */
function answerTheFirstLoad(stats: Stats = STATS) {
  mocks.getStats.mockResolvedValueOnce(stats);
  mocks.listJobs.mockResolvedValueOnce([JOB]);
  mocks.getToBeDelivered.mockResolvedValueOnce(toBeDelivered(5, "T5x 5G"));
  mocks.getOutForDelivery.mockResolvedValueOnce(outForDelivery("482713"));
  mocks.alertsSummary.mockResolvedValueOnce(counts(2, 3));
  mocks.listAlerts.mockResolvedValueOnce([latestAlert(1, "Order OD1 is 3 days overdue")]);
  mocks.listChanges.mockResolvedValue(NO_CHANGES);
}

/** The next request of each panel waits until the test answers it. */
function holdTheNextReplies() {
  const replies = {
    stats: later<Stats>(),
    jobs: later<Job[]>(),
    toBeDelivered: later<ToBeDelivered>(),
    outForDelivery: later<OutForDelivery>(),
    counts: later<AlertSummary>(),
    latest: later<AlertRow[]>(),
  };
  mocks.getStats.mockReturnValueOnce(replies.stats.promise);
  mocks.listJobs.mockReturnValueOnce(replies.jobs.promise);
  mocks.getToBeDelivered.mockReturnValueOnce(replies.toBeDelivered.promise);
  mocks.getOutForDelivery.mockReturnValueOnce(replies.outForDelivery.promise);
  mocks.alertsSummary.mockReturnValueOnce(replies.counts.promise);
  mocks.listAlerts.mockReturnValueOnce(replies.latest.promise);
  return replies;
}

/** Answers each held request in turn, checking that each panel's figures come back with its own reply. */
async function answerEachInTurn(replies: ReturnType<typeof holdTheNextReplies>) {
  await act(async () => {
    replies.stats.resolve({ ...STATS, accounts: 6 });
    replies.jobs.resolve([{ ...JOB, id: 8 }]);
  });
  expect(placeholders()).toEqual({ ...EVERY_PANEL, Totals: false, "Orders by status": false, "Recent jobs": false });
  expect(tile("Accounts")).toBe("6");
  expect(screen.getByText("#8")).toBeTruthy();
  expect(screen.getByRole("img", { name: /Orders by status/ })).toBeTruthy();

  await act(async () => replies.toBeDelivered.resolve(toBeDelivered(7, "Y400")));
  expect(placeholders()["To be delivered"]).toBe(false);
  expect(card("To be delivered").querySelector("h2")?.textContent).toBe("To be delivered — 7 order units across 1 model");
  expect(placeholders()["Out for delivery"]).toBe(true);

  await act(async () => replies.outForDelivery.resolve(outForDelivery("915204")));
  expect(placeholders()["Out for delivery"]).toBe(false);
  expect(within(card("Out for delivery")).getByText("915204")).toBeTruthy();

  // The counts land first, a minute after the first load's; the latest rows
  // that go with them are still on their way.
  vi.setSystemTime(new Date("2026-10-27T09:01:00Z"));
  await act(async () => replies.counts.resolve(counts(4, 5)));
  const attention = card("Needs attention");
  expect(within(attention).getByText(/active ·/).textContent).toBe("4 active · 5 resolved all time");
  const latest = within(attention).getByRole("region", { name: "Latest open alerts" });
  expect(within(latest).getByRole("status", { name: "Loading" })).toBeTruthy();
  expect(within(latest).queryByText("Order OD1 is 3 days overdue")).toBeNull();

  await act(async () => replies.latest.resolve([latestAlert(2, "Order OD2 is 5 days overdue")]));
  expect(within(latest).getByText("Order OD2 is 5 days overdue")).toBeTruthy();
  expect(placeholders()).toEqual(NO_PANEL);
}

describe("Dashboard during a Refresh", () => {
  it("shows placeholders in every panel while the page's Refresh is in flight, then each panel's new figures", async () => {
    stopTheClock();
    answerTheFirstLoad();
    mount();
    await screen.findByText("Order OD1 is 3 days overdue");
    await screen.findByText("482713");
    await screen.findByText(/5 order units across 1 model/);
    expect(placeholders()).toEqual(NO_PANEL);
    expect(firstFigures()).toEqual(EVERY_FIGURE);

    const replies = holdTheNextReplies();
    press("Refresh");

    // Every request is on its way: placeholders in every panel, and none of
    // the figures from before the Refresh — not even for a frame.
    expect(frames.length).toBeGreaterThan(0);
    expect(frames).toEqual(frames.map(() => NO_FIGURE));
    expect(placeholders()).toEqual(EVERY_PANEL);
    expect(firstFigures()).toEqual(NO_FIGURE);
    expect(screen.getByRole("button", { name: "Refreshing…" })).toBeTruthy();
    expect(screen.queryByText(/Could not load|may be stale/)).toBeNull();
    // Each card asked again: the Refresh reloads them all.
    expect(mocks.getToBeDelivered).toHaveBeenCalledTimes(2);
    expect(mocks.getOutForDelivery).toHaveBeenCalledTimes(2);
    expect(mocks.alertsSummary).toHaveBeenCalledTimes(2);

    await answerEachInTurn(replies);
  });

  it("does the same on the Retry after a failed first load", async () => {
    stopTheClock();
    answerTheFirstLoad();
    mocks.getStats.mockReset();
    mocks.getStats.mockRejectedValueOnce(new Error("Server unreachable"));
    mount();
    await screen.findByText("Could not load the dashboard totals");
    await screen.findByText("Order OD1 is 3 days overdue");
    await screen.findByText("482713");
    await screen.findByText(/5 order units across 1 model/);

    // The first load brought the cards' figures, and no totals, chart or jobs.
    expect(firstFigures()).toEqual({ ...EVERY_FIGURE, tiles: false, chart: false, chartTotal: false, job: false });

    const replies = holdTheNextReplies();
    press("Retry");

    expect(frames.length).toBeGreaterThan(0);
    expect(frames).toEqual(frames.map(() => NO_FIGURE));
    expect(placeholders()).toEqual(EVERY_PANEL);
    expect(firstFigures()).toEqual(NO_FIGURE);

    await answerEachInTurn(replies);
    expect(screen.queryByText("Could not load the dashboard totals")).toBeNull();
  });

  it("keeps every figure on screen through the five-second job-progress poll", async () => {
    // Only the intervals are faked: the poll's, and the cards' own reloads.
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    answerTheFirstLoad({ ...STATS, jobs_running: 1 });
    mount();
    await screen.findByText("Order OD1 is 3 days overdue");
    await screen.findByText("482713");
    await screen.findByText(/5 order units across 1 model/);
    expect(screen.getByText(/^Live — updating every 5s/)).toBeTruthy();

    const poll = { stats: later<Stats>(), jobs: later<Job[]>() };
    mocks.getStats.mockReturnValueOnce(poll.stats.promise);
    mocks.listJobs.mockReturnValueOnce(poll.jobs.promise);
    act(() => {
      vi.advanceTimersByTime(5000);
    });

    // The poll asked for the totals and jobs, and nothing else; the figures stay.
    expect(mocks.getStats).toHaveBeenCalledTimes(2);
    expect(mocks.getToBeDelivered).toHaveBeenCalledTimes(1);
    expect(mocks.getOutForDelivery).toHaveBeenCalledTimes(1);
    expect(mocks.alertsSummary).toHaveBeenCalledTimes(1);
    expect(placeholders()).toEqual(NO_PANEL);
    expect(firstFigures()).toEqual(EVERY_FIGURE);
    expect(screen.queryByText("Refreshing…")).toBeNull();

    await act(async () => {
      poll.stats.resolve({ ...STATS, accounts: 6, jobs_running: 0 });
      poll.jobs.resolve([JOB]);
    });
    expect(tile("Accounts")).toBe("6");
    expect(placeholders()).toEqual(NO_PANEL);
  });
});
