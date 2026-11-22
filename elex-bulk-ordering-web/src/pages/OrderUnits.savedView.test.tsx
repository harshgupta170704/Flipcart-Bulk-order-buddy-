// @vitest-environment jsdom
/**
 * A saved view with a relative date range, on the real page (spec FR-011):
 * "Last 7 days" picked, applied and Quick-Saved one day is kept as such, and
 * the page opened on a later day applies the seven days before that day — the
 * Range select still saying "Last 7 days". A range typed by hand stays its
 * fixed dates. The clock is faked (Date only; timers stay real).
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OrderFilters, OrderView } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listOrders: vi.fn(),
  orderFilterOptions: vi.fn(),
  getOrderView: vi.fn(),
  saveOrderView: vi.fn(),
  listJobs: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  ...mocks,
  clearOrderView: vi.fn(),
  downloadOrders: vi.fn(),
  downloadOrdersCsv: vi.fn(),
  downloadInvoice: vi.fn(),
}));
vi.mock("../components/CompileInvoices", () => ({ CompileInvoicesControls: () => null }));
vi.mock("../components/ExtractInvoicesData", () => ({ ExtractInvoicesDataControls: () => null }));

import { orderParams } from "../lib/api";
import OrderUnits from "./OrderUnits";

/** Saturday 24 Oct 2026, and ten days later, in the next month. */
const SAVED_ON = new Date(2026, 9, 24, 15, 30);
const LATER_DAY = new Date(2026, 10, 3, 9, 0);

beforeEach(() => {
  mocks.listOrders.mockResolvedValue({ items: [], total: 0, page: 1, size: 100, pages: 1 });
  mocks.orderFilterOptions.mockResolvedValue({ statuses: [], platform_statuses: [], delivery_states: [], total: 0 });
  mocks.getOrderView.mockResolvedValue(null);
  mocks.listJobs.mockResolvedValue([]);
  mocks.saveOrderView.mockImplementation(async (view: { columns: string[]; filters: Partial<OrderFilters> }) => ({
    ...view,
    saved_at: "2026-10-24T10:00:00",
  }));
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  vi.useRealTimers();
});

function mount() {
  return render(
    <MemoryRouter>
      <OrderUnits />
    </MemoryRouter>,
  );
}

/** The filter of the newest list request. */
function lastAsked(): OrderFilters {
  return mocks.listOrders.mock.calls[mocks.listOrders.mock.calls.length - 1][0];
}

/** Apply what the filter box holds, Quick Save it, and hand back what was saved. */
async function applyAndSave(): Promise<OrderView> {
  fireEvent.click(screen.getByRole("button", { name: "Apply Filters" }));
  await waitFor(() => expect(mocks.listOrders.mock.calls.length).toBeGreaterThan(1));
  fireEvent.click(screen.getByRole("button", { name: "Quick Save" }));
  await waitFor(() => expect(mocks.saveOrderView).toHaveBeenCalled());
  const saved = mocks.saveOrderView.mock.calls[0][0];
  cleanup();
  return { columns: saved.columns, filters: saved.filters, saved_at: "2026-10-24T10:00:00" };
}

/** Open the page again with `view` saved, and wait for the restored filter to be asked for. */
async function reopen(view: OrderView) {
  mocks.listOrders.mockClear();
  mocks.getOrderView.mockResolvedValue(view);
  mount();
  await waitFor(() => expect(screen.getByText("Showing your saved view.")).toBeTruthy());
  await waitFor(() => expect(lastAsked().date_from).toBeTruthy());
}

describe("a saved view's date range", () => {
  it("saved as Last 7 days, reopens on the seven days before the later day", async () => {
    vi.setSystemTime(SAVED_ON);
    mount();
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Range"), { target: { value: "7d" } });

    const view = await applyAndSave();
    expect(view.filters).toEqual({ date_from: "2026-10-18", date_to: "2026-10-24", date_preset: "7d" });

    vi.setSystemTime(LATER_DAY);
    await reopen(view);

    expect(lastAsked()).toMatchObject({ date_from: "2026-10-28", date_to: "2026-11-03" });
    // The preset is the page's own: the request carries the dates alone.
    expect(orderParams(lastAsked()).has("date_preset")).toBe(false);
    expect((screen.getByLabelText("Range") as HTMLSelectElement).value).toBe("7d");
    expect((screen.getByLabelText("Start date") as HTMLInputElement).value).toBe("2026-10-28");
    expect((screen.getByLabelText("End date") as HTMLInputElement).value).toBe("2026-11-03");
  });

  it("picked as This month on the 7th, stays the month though Last 7 days has the same dates", async () => {
    vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
    mount();
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Range"), { target: { value: "month" } });
    expect((screen.getByLabelText("Range") as HTMLSelectElement).value).toBe("month");

    const view = await applyAndSave();
    expect(view.filters).toEqual({ date_from: "2026-10-01", date_to: "2026-10-07", date_preset: "month" });

    vi.setSystemTime(LATER_DAY);
    await reopen(view);

    expect(lastAsked()).toMatchObject({ date_from: "2026-11-01", date_to: "2026-11-03" });
    expect((screen.getByLabelText("Range") as HTMLSelectElement).value).toBe("month");
  });

  it("typed by hand, reopens on the same fixed dates", async () => {
    vi.setSystemTime(SAVED_ON);
    mount();
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Range"), { target: { value: "7d" } });
    // A date typed over the preset's makes the range the operator's own.
    fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-10-02" } });

    const view = await applyAndSave();
    expect(view.filters).toEqual({ date_from: "2026-10-02", date_to: "2026-10-24" });

    vi.setSystemTime(LATER_DAY);
    await reopen(view);

    expect(lastAsked()).toMatchObject({ date_from: "2026-10-02", date_to: "2026-10-24" });
    expect((screen.getByLabelText("Range") as HTMLSelectElement).value).toBe("custom");
  });
});
