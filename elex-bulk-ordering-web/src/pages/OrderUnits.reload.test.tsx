// @vitest-environment jsdom
/**
 * A reload of the ledger page fetches the status picker's choices again (spec
 * FR-009). Refresh, a read-back started from the page, the two uploads and the
 * row dialog's "Read back now" all reload through one path, and any of them
 * can bring a wording the page has not seen — "Order received", say. It must
 * be pickable at once, not only after the page is opened again.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OrderFilterOptions } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listOrders: vi.fn(),
  orderFilterOptions: vi.fn(),
  getOrderView: vi.fn(),
  listJobs: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  ...mocks,
  saveOrderView: vi.fn(),
  clearOrderView: vi.fn(),
  downloadOrders: vi.fn(),
  downloadOrdersCsv: vi.fn(),
  downloadInvoice: vi.fn(),
}));
vi.mock("../components/CompileInvoices", () => ({ CompileInvoicesControls: () => null }));
vi.mock("../components/ExtractInvoicesData", () => ({ ExtractInvoicesDataControls: () => null }));

import OrderUnits from "./OrderUnits";

const BEFORE: OrderFilterOptions = {
  statuses: [],
  platform_statuses: [{ value: "Shipped", count: 2, state: "in_transit" }],
  delivery_states: [],
  total: 2,
};
const AFTER: OrderFilterOptions = {
  ...BEFORE,
  platform_statuses: [...BEFORE.platform_statuses, { value: "Order received", count: 1, state: "unknown" }],
  total: 3,
};

beforeEach(() => {
  mocks.listOrders.mockResolvedValue({ items: [], total: 0, page: 1, size: 100, pages: 1 });
  mocks.getOrderView.mockResolvedValue(null);
  mocks.listJobs.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

async function openPicker() {
  render(
    <MemoryRouter>
      <OrderUnits />
    </MemoryRouter>,
  );
  await waitFor(() => expect(mocks.orderFilterOptions).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "Order status" }));
  expect(await screen.findByRole("button", { name: /^Shipped/ })).toBeTruthy();
}

describe("the status picker after a reload", () => {
  it("offers a wording first read back after the page opened", async () => {
    mocks.orderFilterOptions.mockResolvedValueOnce(BEFORE).mockResolvedValueOnce(AFTER);
    await openPicker();
    expect(screen.queryByRole("button", { name: /^Order received/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(mocks.orderFilterOptions).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("button", { name: /^Order received/ })).toBeTruthy();
    expect(mocks.listOrders.mock.calls.length).toBeGreaterThan(1); // the rows came again too
  });

  it("keeps the choices it has when fetching them again fails", async () => {
    mocks.orderFilterOptions.mockResolvedValueOnce(BEFORE).mockRejectedValueOnce(new Error("Server unreachable"));
    await openPicker();

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(mocks.orderFilterOptions).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mocks.listOrders.mock.calls.length).toBeGreaterThan(1));
    expect(screen.getByRole("button", { name: /^Shipped/ })).toBeTruthy();
  });
});
