// @vitest-environment jsdom
/**
 * A failed export says what failed (spec NFR-009): Export .xlsx and every
 * Download CSV layout end in an "Export failed" toast carrying the server's
 * own words — not a success toast, not a spinner left turning — and the
 * buttons come back for another try. Underneath, the api module's reading of
 * a refused download is checked too: the server's `detail` is the message
 * the toast shows.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listOrders: vi.fn(),
  orderFilterOptions: vi.fn(),
  getOrderView: vi.fn(),
  listJobs: vi.fn(),
  downloadOrders: vi.fn(),
  downloadOrdersCsv: vi.fn(),
}));

// The real module with the page's own calls replaced: ApiError and
// errorMessage stay real, so the toast reads what the page would show.
vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  ...mocks,
  saveOrderView: vi.fn(),
  clearOrderView: vi.fn(),
  downloadInvoice: vi.fn(),
}));
// Toolbar panels with fetches of their own, beside the point here.
vi.mock("../components/CompileInvoices", () => ({ CompileInvoicesControls: () => null }));
vi.mock("../components/ExtractInvoicesData", () => ({ ExtractInvoicesDataControls: () => null }));

import { ApiError } from "../lib/api";
import OrderUnits from "./OrderUnits";

/** One ledger row, so the page has something to export. */
function unit(id: number): Order {
  return {
    id,
    order_row_id: id,
    unit_position: 1,
    units_in_order: 1,
    source: "run",
    account_email: "units@test.com",
    product_link: null,
    qty: 1,
    model: "vivo T5x 5G",
    color: null,
    pin: "110001",
    amount: 15000,
    target: null,
    order_id: `OD${String(id).padStart(18, "0")}`,
    gst_name: null,
    status: "placed",
    payment_mode: "card",
    card_number: "4111 1111 1111 1111",
    delivery_address: null,
    delivery_pincode: null,
    billed_gstin: null,
    billed_business_name: null,
    placed_at: "2026-10-24T10:00:00",
    created_at: "2026-10-24T10:00:00",
    brand: null,
    category: null,
    job_id: 1,
    delivery_state: "in_transit",
    item_id: null,
    unit_id: null,
    seller: null,
    tracking_id: null,
    platform_status: "Shipped",
    delivery_date: null,
    delivery_otp: null,
    invoice_url: null,
    invoice_on_file: false,
    synced_at: "2026-10-25T08:00:00",
    open_request_kinds: [],
  };
}

beforeEach(() => {
  mocks.listOrders.mockResolvedValue({ items: [unit(1)], total: 1, page: 1, size: 100, pages: 1 });
  mocks.orderFilterOptions.mockResolvedValue({ statuses: [], platform_statuses: [], delivery_states: [], total: 1 });
  mocks.getOrderView.mockResolvedValue(null);
  mocks.listJobs.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function exportButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Export .xlsx" }) as HTMLButtonElement;
}

/** The ledger with its one row loaded, the exports offered. */
async function openLedger() {
  render(
    <MemoryRouter initialEntries={["/order-units"]}>
      <Routes>
        <Route path="/order-units" element={<OrderUnits />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() => expect(exportButton().disabled).toBe(false));
}

async function lastToast(): Promise<string> {
  const toasts = await screen.findAllByRole("alert");
  return toasts[toasts.length - 1].textContent ?? "";
}

describe("a failed export", () => {
  it("says Export failed with the server's text when the workbook is refused", async () => {
    mocks.downloadOrders.mockRejectedValue(new ApiError(500, "the workbook could not be written: no space left on device"));
    await openLedger();

    fireEvent.click(exportButton());

    const toast = await lastToast();
    expect(toast).toContain("Export failed");
    expect(toast).toContain("the workbook could not be written: no space left on device");
    expect(mocks.downloadOrders).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Workbook downloaded")).toBeNull();
    // Nothing left spinning: the export can be tried again.
    await waitFor(() => expect(exportButton().disabled).toBe(false));
  });

  it("says Export failed with the server's text for each CSV layout", async () => {
    mocks.downloadOrdersCsv
      .mockRejectedValueOnce(new ApiError(503, "the database is busy — try the export again in a minute"))
      .mockRejectedValueOnce(new ApiError(422, "sheet must be one of ['order_data', 'out_for_delivery']"));
    await openLedger();

    fireEvent.click(screen.getByRole("button", { name: "Download CSV" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Full ledger CSV/ }));
    const first = await lastToast();
    expect(first).toContain("Export failed");
    expect(first).toContain("the database is busy — try the export again in a minute");

    await waitFor(() => expect((screen.getByRole("button", { name: "Download CSV" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Download CSV" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Out for delivery sheet/ }));
    await waitFor(() => expect(mocks.downloadOrdersCsv).toHaveBeenCalledTimes(2));
    const second = await lastToast();
    expect(second).toContain("Export failed");
    expect(second).toContain("sheet must be one of ['order_data', 'out_for_delivery']");
    expect(mocks.downloadOrdersCsv.mock.calls[1][1]).toBe("out_for_delivery");
    expect(screen.queryByText(/downloaded$/)).toBeNull();
  });
});

describe("a refused download, as the api module reads it", () => {
  it("carries the server's detail as the message, for the workbook and the CSV alike", async () => {
    const api = await vi.importActual<typeof import("../lib/api")>("../lib/api");
    const refusal = () =>
      new Response(JSON.stringify({ detail: "the export could not be read from the database" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    vi.stubGlobal("fetch", vi.fn(async () => refusal()));

    await expect(api.downloadOrders({})).rejects.toMatchObject({
      status: 500,
      message: "the export could not be read from the database",
    });
    await expect(api.downloadOrdersCsv({})).rejects.toMatchObject({
      status: 500,
      message: "the export could not be read from the database",
    });
  });

  it("says the server could not be reached when the request never got an answer", async () => {
    const api = await vi.importActual<typeof import("../lib/api")>("../lib/api");
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));

    await expect(api.downloadOrdersCsv({})).rejects.toMatchObject({
      status: 0,
      message: "Could not reach the server. Check that it is running and try again.",
    });
  });
});
