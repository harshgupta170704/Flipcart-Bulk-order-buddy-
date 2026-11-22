// @vitest-environment jsdom
/**
 * Dates typed by hand are the operator's own range, on the real page (review
 * of 27 Oct 2026). Typed so that they happen to equal a Range preset today —
 * the seven days up to today, or the month so far — the Range select used to
 * say "Last 7 days", and Quick Save kept the preset, so the view reopened a
 * week later on that week's dates instead of the ones typed. Only a preset
 * picked from the select is kept as one now; typed dates say "Custom range"
 * and are saved as the dates. The clock is faked (Date only; timers stay real).
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

function lastAsked(): OrderFilters {
  return mocks.listOrders.mock.calls[mocks.listOrders.mock.calls.length - 1][0];
}

function range(): string {
  return (screen.getByLabelText("Range") as HTMLSelectElement).value;
}

function type(label: "Start date" | "End date", value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

async function applyAndSave(): Promise<OrderView> {
  fireEvent.click(screen.getByRole("button", { name: "Apply Filters" }));
  await waitFor(() => expect(mocks.listOrders.mock.calls.length).toBeGreaterThan(1));
  fireEvent.click(screen.getByRole("button", { name: "Quick Save" }));
  await waitFor(() => expect(mocks.saveOrderView).toHaveBeenCalled());
  const saved = mocks.saveOrderView.mock.calls[0][0];
  cleanup();
  return { columns: saved.columns, filters: saved.filters, saved_at: "2026-10-24T10:00:00" };
}

async function reopen(view: OrderView) {
  mocks.listOrders.mockClear();
  mocks.getOrderView.mockResolvedValue(view);
  mount();
  await waitFor(() => expect(screen.getByText("Showing your saved view.")).toBeTruthy());
  await waitFor(() => expect(lastAsked().date_from).toBeTruthy());
}

describe("dates typed by hand that equal a preset today", () => {
  it("the seven days up to today: Quick Save keeps those dates, and they reopen a week later", async () => {
    vi.setSystemTime(SAVED_ON);
    mount();
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    type("Start date", "2026-10-18");
    type("End date", "2026-10-24");

    const view = await applyAndSave();
    expect(view.filters).toEqual({ date_from: "2026-10-18", date_to: "2026-10-24" });

    vi.setSystemTime(LATER_DAY);
    await reopen(view);

    expect(lastAsked()).toMatchObject({ date_from: "2026-10-18", date_to: "2026-10-24" });
    expect(range()).toBe("custom");
  });

  it("the month so far: Quick Save keeps the dates", async () => {
    vi.setSystemTime(SAVED_ON);
    mount();
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    type("Start date", "2026-10-01");
    type("End date", "2026-10-24");

    const view = await applyAndSave();
    expect(view.filters).toEqual({ date_from: "2026-10-01", date_to: "2026-10-24" });
  });

  it("the Range select says Custom range for them, not the preset they equal", async () => {
    vi.setSystemTime(SAVED_ON);
    mount();
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    type("Start date", "2026-10-18");
    type("End date", "2026-10-24");
    expect(range()).toBe("custom");
    type("Start date", "2026-10-01");
    expect(range()).toBe("custom");
  });

  it("a picked preset's date typed away and back is the operator's range from then on", async () => {
    vi.setSystemTime(SAVED_ON);
    mount();
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Range"), { target: { value: "7d" } });
    expect(range()).toBe("7d");
    type("Start date", "2026-10-17");
    type("Start date", "2026-10-18"); // Last 7 days' own start again, typed
    expect(range()).toBe("custom");

    const view = await applyAndSave();
    expect(view.filters).toEqual({ date_from: "2026-10-18", date_to: "2026-10-24" });
  });

  it("a preset picked from the select is still kept as one", async () => {
    vi.setSystemTime(SAVED_ON);
    mount();
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Range"), { target: { value: "30d" } });
    expect(range()).toBe("30d");

    const view = await applyAndSave();
    expect(view.filters).toEqual({ date_from: "2026-09-25", date_to: "2026-10-24", date_preset: "30d" });
  });
});
