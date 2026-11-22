// @vitest-environment jsdom
/**
 * The ledger table's Outcome column: an order Flipkart took and cancelled
 * later reads "Cancelled by Flipkart", with Flipkart's reason on hover; the
 * red "Failed" stays for a run that never sent anything.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";

vi.mock("../lib/api", () => ({ downloadInvoice: vi.fn() }));

import { OrderTable } from "./OrderTable";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const SELLER_POLICY = "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand.";

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
};

/** The Outcome cell's text for each row, in row order. */
function outcomes(): string[] {
  return screen.getAllByRole("row").slice(1).map((row) => row.querySelectorAll("td")[0]?.textContent?.trim() ?? "");
}

describe("the Outcome column", () => {
  it("tells a Flipkart cancellation from the run's own failure", () => {
    render(
      <OrderTable
        {...props}
        orders={[
          unit({ id: 1, status: "failed", order_id: null, platform_status: null, synced_at: null, delivery_state: "unknown" }),
          unit({
            id: 2,
            status: "failed",
            platform_status: "Cancelled",
            delivery_state: "cancelled",
            cancel_reason: SELLER_POLICY,
            cancelled_by: "flipkart",
          }),
          unit({
            id: 3,
            status: "failed",
            platform_status: "Cancelled",
            delivery_state: "cancelled",
            cancel_reason: "You requested a cancellation because you changed your mind about this product.",
            cancelled_by: "user",
          }),
          unit({ id: 4 }),
        ]}
      />,
    );

    expect(outcomes()).toEqual(["✕Failed", "⊘Cancelled by Flipkart", "⊘Cancelled by user", "✓Placed"]);
    // Flipkart's reason is on the cell, for a hover; nothing for the run's failure.
    const cancelled = screen.getByText("Cancelled by Flipkart").closest("[title]");
    expect(cancelled?.getAttribute("title")).toBe(SELLER_POLICY);
    expect(screen.getByText("Failed").closest("[title]")).toBeNull();
  });

  it("reads a cancelled order as Flipkart's even before the server says who cancelled", () => {
    render(<OrderTable {...props} orders={[unit({ id: 5, status: "failed", platform_status: "Cancelled", delivery_state: "cancelled" })]} />);
    expect(outcomes()).toEqual(["⊘Cancelled by Flipkart"]);
  });
});
