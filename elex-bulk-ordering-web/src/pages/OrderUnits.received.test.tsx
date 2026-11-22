// @vitest-environment jsdom
/**
 * "Mark received" and "Not received" on the ledger's toolbar: the ticked rows'
 * goods marked as in hand, or the mark taken back (spec FR-001's "Received"),
 * and the page reloads to show it.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listOrders: vi.fn(),
  orderFilterOptions: vi.fn(),
  getOrderView: vi.fn(),
  listJobs: vi.fn(),
  markReceived: vi.fn(),
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

/** One ledger row; override what a test is about. */
function unit(over: Partial<Order> & { id: number }): Order {
  return {
    order_row_id: over.id,
    unit_position: 1,
    units_in_order: 1,
    source: "run",
    account_email: "units@test.com",
    product_link: null,
    qty: 1,
    model: "vivo T5 Lite 44W 5G",
    color: null,
    pin: "110001",
    amount: 18207,
    target: 18250,
    order_id: `OD${String(over.id).padStart(18, "0")}`,
    gst_name: null,
    status: "placed",
    payment_mode: "cod",
    card_number: null,
    delivery_address: null,
    delivery_pincode: null,
    billed_gstin: null,
    billed_business_name: null,
    placed_at: "2026-10-24T10:00:00",
    created_at: "2026-10-24T10:00:00",
    brand: "vivo",
    category: null,
    job_id: 1,
    delivery_state: "in_transit",
    item_id: null,
    unit_id: null,
    seller: "HatmanRetails",
    tracking_id: null,
    platform_status: "Order Confirmed",
    delivery_date: null,
    delivery_otp: null,
    invoice_url: null,
    invoice_on_file: false,
    synced_at: "2026-10-25T08:00:00",
    open_request_kinds: [],
    ...over,
  };
}

beforeEach(() => {
  mocks.listOrders.mockResolvedValue({ items: [unit({ id: 11 }), unit({ id: 12 })], total: 2, page: 1, size: 100, pages: 1 });
  mocks.orderFilterOptions.mockResolvedValue({ statuses: [], platform_statuses: [], delivery_states: [], total: 2 });
  mocks.getOrderView.mockResolvedValue(null);
  mocks.listJobs.mockResolvedValue([]);
  mocks.markReceived.mockResolvedValue({ changed: 1, missing: [] });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("Mark received", () => {
  it("marks the ticked rows and reloads; Not received takes it back", async () => {
    render(
      <MemoryRouter>
        <OrderUnits />
      </MemoryRouter>,
    );
    const mark = await screen.findByRole("button", { name: "Mark received" });
    expect((mark as HTMLButtonElement).disabled).toBe(true); // nothing ticked yet

    await waitFor(() => expect(screen.getAllByRole("checkbox").length).toBeGreaterThan(2));
    const rows = screen.getAllByRole("row").slice(1);
    fireEvent.click(within(rows[1]).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Mark received" }));

    await waitFor(() => expect(mocks.markReceived).toHaveBeenCalledWith([12], true));
    await waitFor(() => expect(mocks.listOrders.mock.calls.length).toBeGreaterThan(1));
    expect(await screen.findByText("Marked received")).toBeTruthy();

    fireEvent.click(within(screen.getAllByRole("row").slice(1)[0]).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Not received" }));
    await waitFor(() => expect(mocks.markReceived).toHaveBeenCalledWith([11], false));
  });
});
