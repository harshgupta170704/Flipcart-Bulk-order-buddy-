// @vitest-environment jsdom
/**
 * A row's Cancel button: offered on an order that may be on its way, never on
 * an attempt Flipkart never took (spec FR-020; review of 27 Oct 2026). The
 * server skips such a request too, with its reason.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";

const mocks = vi.hoisted(() => ({ downloadInvoice: vi.fn() }));
vi.mock("../lib/api", () => ({ downloadInvoice: mocks.downloadInvoice }));

import { OrderTable } from "./OrderTable";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

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

const props = {
  loading: false,
  error: null,
  onRetry: () => undefined,
  filtered: false,
  onClearFilter: () => undefined,
  visibleColumns: ["status", "platform_status", "order_id"],
  onCancel: () => undefined,
};

describe("the row's Cancel button", () => {
  it("is offered on a placed order on its way, and on an unconfirmed one", () => {
    render(<OrderTable {...props} orders={[unit({ id: 1 }), unit({ id: 2, status: "unconfirmed", order_id: null })]} />);
    expect(screen.getAllByRole("button", { name: /^Cancel / })).toHaveLength(2);
  });

  it("is not offered on an attempt Flipkart never took", () => {
    render(
      <OrderTable
        {...props}
        orders={[
          unit({ id: 3, status: "failed", order_id: null, delivery_state: "unknown", platform_status: null }),
          unit({ id: 4, status: "failed_over_target", order_id: null, delivery_state: undefined, platform_status: null }),
        ]}
      />,
    );
    expect(screen.queryAllByRole("button", { name: /^Cancel / })).toHaveLength(0);
  });
});

describe("the row's invoice button", () => {
  it("passes on why a download failed, so the page can say so", async () => {
    mocks.downloadInvoice.mockRejectedValue(new Error("404 the PDF is not in the store"));
    const failed = vi.fn();
    render(
      <OrderTable
        {...props}
        visibleColumns={["status", "invoice"]}
        onInvoiceDownloadError={failed}
        orders={[unit({ id: 5, invoice_on_file: true })]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Download the invoice/ }));
    await waitFor(() => expect(failed).toHaveBeenCalledWith("404 the PDF is not in the store"));
  });
});

describe("the IMEI column", () => {
  it("shows a unit's IMEI, and marks one that is the order's", () => {
    render(
      <OrderTable
        {...props}
        visibleColumns={["status", "imei", "unit_amount"]}
        orders={[
          unit({ id: 6, imei: "358107262645105", imei_scope: "unit", unit_amount: 18207 }),
          unit({ id: 7, imei: "358107262645113, 358107262645121", imei_scope: "order" }),
        ]}
      />,
    );
    expect(screen.getByText("358107262645105")).toBeTruthy();
    expect(screen.getByText("for the order")).toBeTruthy();
    expect(screen.getByText("₹18,207.00")).toBeTruthy();
  });
});
